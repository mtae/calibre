#!/usr/bin/env python
# ruff: noqa: E501
# License: GPLv3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

"""
Tests for catastrophic backtracking in the regular expressions used by the
conversion heuristics (conversion/utils.py, conversion/preprocess.py) and the
TXT/Markdown/Textile processors (txt/).

Two kinds of test live here.

1. Timing tests. Each runs one entry point on one adversarial input (a long run
   of whitespace, unclosed tags, a near miss that makes the engine backtrack)
   in a forked child process that is killed if it takes too long. The input
   size is chosen so that an implementation whose running time is (close to)
   linear in the size of the input finishes in a few milliseconds, so a
   quadratic or worse implementation fails. Cases that are known to be too
   slow are listed in SLOW_CASES with the measured growth and the location of
   the guilty pattern and are run as expected failures: when one is fixed its
   test starts passing and the unittest "unexpected success" tells you to take
   it out of the table.

2. A lint over the regular expressions themselves. It looks at the parse tree
   of every pattern (not at the text), so it understands character classes,
   groups and lookarounds. See the docstring of Linter for the rules. The
   patterns that are known to break the rules are listed in KNOWN_OFFENDERS;
   the test fails for any new offender and also for any entry in the table
   that is no longer an offender, so the table can only shrink.

To add a timing case, put an (entry point, family, size) in PASSING_CASES, or, if it is too slow, in SLOW_CASES with a note on the
growth, a measured time and the location of the pattern. ENTRY_POINTS and FAMILIES are the tables of what can be run and on what.
`calibre-debug -c "from calibre.ebooks.conversion.regex_safety_test import measure; measure()"` prints the CPU time of every
case (and `measure(n)` for entry point and family at another size) to find sizes for new cases.

Run the lint by hand, to see the offenders, with
    calibre-debug -c "from calibre.ebooks.conversion.regex_safety_test import main; main()"
"""

import ast
import multiprocessing
import os
import re
import sys
import time
import unittest
from collections import defaultdict, namedtuple
from re import _constants as sre_constants
from re import _parser as sre_parser
from types import SimpleNamespace

# ----------------------------------------------------------------------------
# Lint
# ----------------------------------------------------------------------------

MAXREPEAT = sre_constants.MAXREPEAT
# Characters that stand in for "any character" when intersecting character classes
UNIVERSE = frozenset(list(range(0x250)) + [0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x3000, 0x4E00, 0x10348])
EMPTY = frozenset()
WHITESPACE = frozenset(c for c in UNIVERSE if chr(c).isspace())
_CATEGORY_TESTS = {
    'CATEGORY_DIGIT': str.isdecimal,
    'CATEGORY_SPACE': str.isspace,
    'CATEGORY_WORD': lambda ch: ch.isalnum() or ch == '_',
    'CATEGORY_LINEBREAK': lambda ch: ch == '\n',
}


def category_chars(category):
    name = str(category)
    positive = name.replace('CATEGORY_NOT_', 'CATEGORY_').replace('CATEGORY_UNI_NOT_', 'CATEGORY_').replace('CATEGORY_UNI_', 'CATEGORY_')
    test = _CATEGORY_TESTS[positive]
    negate = positive != name and 'NOT' in name
    return frozenset(c for c in UNIVERSE if bool(test(chr(c))) != negate)


def fold_case(chars, ignorecase):
    if not ignorecase:
        return frozenset(chars)
    ans = set(chars)
    for c in chars:
        for v in (chr(c).lower(), chr(c).upper()):
            if len(v) == 1 and ord(v) in UNIVERSE:
                ans.add(ord(v))
    return frozenset(ans)


def single_char_set(op, av, flags):
    """The characters matched by a node that matches exactly one character, else None"""
    ignorecase = bool(flags & re.IGNORECASE)
    if op is sre_constants.LITERAL:
        return fold_case({av}, ignorecase)
    if op is sre_constants.NOT_LITERAL:
        return UNIVERSE - fold_case({av}, ignorecase)
    if op is sre_constants.ANY:
        return UNIVERSE if flags & re.DOTALL else UNIVERSE - {10}
    if op is sre_constants.IN:
        chars, negate = set(), False
        for iop, iav in av:
            if iop is sre_constants.NEGATE:
                negate = True
            elif iop is sre_constants.LITERAL:
                chars.add(iav)
            elif iop is sre_constants.RANGE:
                chars.update(c for c in UNIVERSE if iav[0] <= c <= iav[1])
            elif iop is sre_constants.CATEGORY:
                chars |= category_chars(iav)
        chars = fold_case(chars, ignorecase)
        return UNIVERSE - chars if negate else chars
    return None


class Info:
    """What the linter needs to know about a node (or a sequence of nodes)

    nullable: can match the empty string
    first, last: characters that can be the first/last character matched
    ub_first, ub_last: characters that the left/right edge can consume as part
        of a repeat without an upper limit, which is what the regex engine
        will give back one by one when what follows fails to match
    sealed_last: characters in last that are only ever matched by a possessive
        repeat at the right edge, so the engine never gives them back and
        whatever follows (or the next iteration of a repeated group) cannot
        take some of them
    """

    __slots__ = ('chars', 'first', 'last', 'nullable', 'sealed_last', 'ub_first', 'ub_last')

    def __init__(self, nullable=True, first=EMPTY, last=EMPTY, ub_first=EMPTY, ub_last=EMPTY, chars=EMPTY, sealed_last=EMPTY):
        self.nullable, self.first, self.last, self.ub_first, self.ub_last, self.chars = nullable, first, last, ub_first, ub_last, chars
        self.sealed_last = sealed_last


def describe_chars(chars):
    if chars >= WHITESPACE and len(chars) <= len(WHITESPACE) + 3:
        return 'whitespace'
    if len(chars) > 30:
        return f'{len(chars)} different characters'
    return ''.join(chr(c) if 32 < c < 127 else f'\\x{c:02x}' for c in sorted(chars))


class Linter:
    """
    Finds sub-expressions that make the regex engine backtrack in polynomial
    or exponential time when a match fails. It reports:

    stacked
        Two unbounded repeats (``*``, ``+``, ``{n,}``, greedy or lazy) that
        are only separated by things that can match nothing (``?`` groups,
        ``*`` repeats, lookarounds) and that can consume the same character.
        ``\\s*\\s*``, ``.*?\\s*``, ``\\s*(</b>\\s*)?\\s*`` and ``(x\\s*)*\\s*`` are
        examples. A run of n characters can be split between the two repeats
        in n ways, and the engine tries every one of them when what follows
        does not match. With k such repeats in a row it is n**k. A lookahead
        that excludes the shared characters (``\\s*(?!\\s)``) or a possessive
        quantifier/atomic group (``\\s*+``) removes the problem and is
        understood by the linter.
    nested
        A group that is repeated, and that can both start and end with an
        unbounded repeat over the same character: ``(a+)+``, ``(\\w+\\s*)*``,
        ``(\\s*<p>\\s*)*``. The same run of characters can be divided between
        the iterations in many ways. Exponential if the repeat count of the
        group is unbounded, polynomial of the degree of the repeat count
        otherwise.

    Not found: the quadratic cost of a pattern that starts with an unbounded
    repeat and is searched for in a long run of that character
    (``re.sub(r'\\s*</p>', ...)`` on a lot of whitespace), and plain
    alternation ambiguity (``(a|a)*``). The timing tests cover the first.
    """

    def __init__(self):
        self.findings = []

    def report(self, rule, detail):
        self.findings.append((rule, detail))

    def sequence(self, items, flags):
        infos, restrictions = [], []
        for op, av in items:
            infos.append(self.node(op, av, flags))
            restrictions.append(self.lookaround_restriction(op, av, flags))
        for j, info_j in enumerate(infos):
            if not info_j.ub_first:
                continue
            for i in range(j - 1, -1, -1):
                overlap = infos[i].ub_last & info_j.ub_first
                if overlap:
                    # Single character lookarounds between the repeats can exclude the shared characters
                    for k in range(i + 1, j):
                        r = restrictions[k]
                        if r is not None:
                            overlap = overlap & r
                if overlap:
                    self.report('stacked', f'unbounded repeats {i} and {j} can both consume {describe_chars(overlap)}')
                if not infos[i].nullable:
                    break
        return self.combine(infos)

    @staticmethod
    def lookaround_restriction(op, av, flags):
        """The set of characters a lookahead/lookbehind of a single character allows next to it, or None"""
        if op not in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
            return None
        body = av[1]
        if len(body) != 1:
            return None
        chars = single_char_set(body[0][0], body[0][1], flags)
        if chars is None:
            return None
        return chars if op is sre_constants.ASSERT else UNIVERSE - chars

    @staticmethod
    def combine(infos):
        first, ub_first, last, ub_last, chars, sealed, unsealed = set(), set(), set(), set(), set(), set(), set()
        for info in infos:
            first |= info.first
            ub_first |= info.ub_first
            if not info.nullable:
                break
        for info in reversed(infos):
            last |= info.last
            ub_last |= info.ub_last
            sealed |= info.sealed_last
            unsealed |= info.last - info.sealed_last
            if not info.nullable:
                break
        for info in infos:
            chars |= info.chars
        return Info(
            all(i.nullable for i in infos), frozenset(first), frozenset(last), frozenset(ub_first), frozenset(ub_last), frozenset(chars),
            frozenset(sealed - unsealed),
        )

    def node(self, op, av, flags):
        chars = single_char_set(op, av, flags)
        if chars is not None:
            return Info(False, chars, chars, EMPTY, EMPTY, chars)
        if op in (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT, sre_constants.POSSESSIVE_REPEAT):
            lo, hi, body = av
            b = self.sequence(body, flags)
            possessive = op is sre_constants.POSSESSIVE_REPEAT
            if hi == MAXREPEAT and b.chars:
                # Any iteration can start or end with any character that the body can start or end with,
                # except that a possessive repeat at the end of the body takes all of the characters it can
                ub_first, ub_last = b.first, EMPTY if possessive else b.last - b.sealed_last
            else:
                ub_first, ub_last = b.ub_first, EMPTY if possessive else b.ub_last
            if hi > 1 and b.ub_last & b.ub_first:
                self.report('nested', f'repeated group can start and end with an unbounded repeat over {describe_chars(b.ub_last & b.ub_first)}')
            sealed_last = b.last if possessive and hi == MAXREPEAT else b.sealed_last
            return Info(lo == 0 or b.nullable, b.first, b.last, ub_first, ub_last, b.chars, sealed_last)
        if op is sre_constants.SUBPATTERN:
            group, add_flags, del_flags, body = av
            return self.sequence(body, (flags | add_flags) & ~del_flags)
        if op is sre_constants.ATOMIC_GROUP:
            b = self.sequence(av, flags)
            return Info(b.nullable, b.first, b.last, b.ub_first, EMPTY, b.chars)
        if op is sre_constants.BRANCH:
            infos = [self.sequence(alternative, flags) for alternative in av[1]]
            return self.union(infos)
        if op is sre_constants.GROUPREF_EXISTS:
            infos = [self.sequence(av[1], flags), self.sequence(av[2], flags) if av[2] else Info(True)]
            return self.union(infos)
        if op in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
            self.sequence(av[1], flags)
            return Info(True)
        if op is sre_constants.AT:
            return Info(True)
        if op is sre_constants.GROUPREF:
            return Info(False)
        raise ValueError(f'Unknown regular expression opcode: {op}')

    @staticmethod
    def union(infos):
        def u(attr):
            return frozenset().union(*(getattr(i, attr) for i in infos))

        sealed = u('sealed_last') - frozenset().union(*(i.last - i.sealed_last for i in infos))
        return Info(any(i.nullable for i in infos), u('first'), u('last'), u('ub_first'), u('ub_last'), u('chars'), sealed)


def lint_pattern(pattern, flags=0):
    """Return a list of (rule, description) for a pattern (a str)"""
    parsed = sre_parser.parse(pattern, flags)
    linter = Linter()
    linter.sequence(parsed, parsed.state.flags)
    return linter.findings


def normalize_pattern(pattern):
    # Patterns that depend on the median line length only differ in a number
    return re.sub(r'\(\?<=\.\{\d+\}', '(?<=.{N}', pattern)


def conversion_source_dirs():
    import calibre.ebooks.conversion
    import calibre.ebooks.txt

    return [os.path.dirname(m.__file__) for m in (calibre.ebooks.conversion, calibre.ebooks.txt)]


def relative_path(path):
    path = path.replace(os.sep, '/')
    return path[path.rindex('/calibre/') + len('/calibre/') :] if '/calibre/' in path else path


def in_scope(path):
    path = path.replace(os.sep, '/')
    return '/calibre/ebooks/conversion/' in path or '/calibre/ebooks/txt/' in path


def toplevel_function(qualname):
    return qualname.split('.<locals>')[0]


_RE_FUNCS = {'compile': 1, 'search': 2, 'match': 2, 'fullmatch': 2, 'findall': 2, 'finditer': 2, 'sub': 4, 'subn': 4, 'split': 3}


def fold_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = fold_string(node.left), fold_string(node.right)
        if a is not None and b is not None:
            return a + b
    return None


def eval_flags(node):
    if node is None:
        return 0
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 're':
        return int(getattr(re, node.attr))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return eval_flags(node.left) | eval_flags(node.right)
    return None


def static_patterns(path):
    """Regular expressions that are string literals in calls to re.compile(), re.sub(), etc. in a file"""
    with open(path, 'rb') as f:
        tree = ast.parse(f.read(), path)
    ans = []
    rel = relative_path(path)

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if scope and scope[-1][1]:  # a function nested in a function counts as its parent
                new_scope = scope
            else:
                new_scope = scope + [(node.name, isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))]
            for child in ast.iter_child_nodes(node):
                visit(child, new_scope)
            return
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == 're' and func.attr in _RE_FUNCS and node.args:
                pattern = fold_string(node.args[0])
                if pattern is not None:
                    flag_node = None
                    for kw in node.keywords:
                        if kw.arg == 'flags':
                            flag_node = kw.value
                    pos = _RE_FUNCS[func.attr]
                    if flag_node is None and len(node.args) > pos:
                        flag_node = node.args[pos]
                    flags = eval_flags(flag_node)
                    if flags is not None:
                        ans.append((pattern, flags, rel, '.'.join(s[0] for s in scope) or '<module>', node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, [])
    return ans


class PatternRecorder:
    """Records the patterns compiled while it is active, which finds the patterns assembled from fragments at run time"""

    def __init__(self):
        self.patterns = []
        self.re_dir = os.path.dirname(re.__file__)

    def __enter__(self):
        self.orig = re._compile
        recorder = self

        def _compile(pattern, flags=0):
            ans = recorder.orig(pattern, flags)
            if isinstance(pattern, str):
                f = sys._getframe(1)
                while f is not None and f.f_code.co_filename.startswith(recorder.re_dir):
                    f = f.f_back
                if f is not None and in_scope(f.f_code.co_filename):
                    recorder.patterns.append((pattern, int(flags), relative_path(f.f_code.co_filename), toplevel_function(f.f_code.co_qualname), f.f_lineno))
            return ans

        re._compile = _compile
        return self

    def __exit__(self, *a):
        re._compile = self.orig


HTML_CHAPTERS = ''.join(
    f'<p>CHAPTER {i}</p>\n<p>Some title words</p>\n<p>{"Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 5}</p>\n' for i in range(1, 6)
)
PADDING = '<p>' + 'lorem ipsum dolor sit amet ' * 20 + '</p>\n'
SAMPLE_DOCUMENTS = (
    PADDING * 3,
    HTML_CHAPTERS,
    ('<p>' + 'Lorem ipsum dolor sit amet ' * 5 + '</p>\n<p> </p>\n') * 30 + '<div></div>\n<p></p><div></div><p></p>\n',
    PADDING + '<p>* * *</p>\n' + PADDING + '<p>&nbsp;</p><div></div>' + PADDING + '<h2>Head</h2><p></p>' + PADDING,
    ('<p>' + 'word ' * 10 + 'inter-</p>\n<p>national ' + 'word ' * 12 + ',</p>\n') * 10 + 'inter-\nnational ' * 10,
    PADDING + '<p>_ital_ etc. e.g. *bold* /slash/</p>\n<p>&nbsp;&nbsp;&nbsp;indented</p><o:p></o:p><span><span> </span></span>',
    '<pre>' + 'line one\n\nline two\n' * 20 + '</pre>',
    ''.join(f'<p>{i}. Title words</p>\n{PADDING}' for i in range(1, 8)),
    ''.join(f'<p><b>Heading {i}</b></p>\n{PADDING}' for i in range(1, 8)),
    ''.join(f'<p>THE TITLE {i}</p>\n{PADDING}' for i in range(1, 8)),
    ''.join(f'<p>A B C D {i}</p>\n{PADDING}' for i in range(1, 8)),
    '<h1>a</h1><h2>b</h2>' + PADDING,
    'Para one line one\nline two\n\n    Indented para here\n\nChapter 1\n=========\n\n# Head\n\n* a\n* b\n\nh1. Textile\n\n"link":http://x !img.png!\n' * 3,
)
PDFTOHTML_MARKER = "<!-- created by calibre's pdftohtml -->\n"
ABBYY_MARKER = '<meta name="generator" content="ABBYY FineReader">\n'


class QuietLog:
    def __getattr__(self, name):
        return lambda *a, **kw: None


def make_options(**kw):
    ans = dict(
        verbose=0, fix_indents=False, markup_chapter_headings=False, italicize_common_cases=False, delete_blank_paragraphs=False,
        html_unwrap_factor=0.4, unwrap_lines=False, dehyphenate=False, renumber_headings=False, format_scene_breaks=False,
        replace_scene_breaks='', enable_heuristics=False, debug_pipeline=None, unwrap_factor=0.0, keep_ligatures=False, asciiize=False,
        smarten_punctuation=False, sr1_search='', sr1_replace='', sr2_search='', sr2_replace='', sr3_search='', sr3_replace='',
        search_replace=None, remove_paragraph_spacing=False, max_line_length=0, force_max_line_length=False, keep_links=False,
    )  # fmt: skip
    ans.update(kw)
    return SimpleNamespace(**ans)


ALL_HEURISTICS = dict(
    fix_indents=True, markup_chapter_headings=True, italicize_common_cases=True, delete_blank_paragraphs=True, unwrap_lines=True,
    dehyphenate=True, renumber_headings=True, format_scene_breaks=True, replace_scene_breaks='* * *', enable_heuristics=True,
)  # fmt: skip


def heuristic_processor(**kw):
    from calibre.ebooks.conversion.utils import HeuristicProcessor

    return HeuristicProcessor(make_options(**kw), QuietLog())


def run_everything_once(documents=SAMPLE_DOCUMENTS):
    """Run all the entry points that use regular expressions on some documents, to record the patterns they compile"""
    from calibre.ebooks.conversion import preprocess
    from calibre.ebooks.conversion.preprocess import CSSPreProcessor, Dehyphenator, DocAnalysis, HTMLPreProcessor
    from calibre.ebooks.txt import processor as txt
    from calibre.ebooks.txt.markdownml import MarkdownMLizer
    from calibre.ebooks.txt.textileml import TextileMLizer
    from calibre.ebooks.txt.txtml import TXTMLizer

    for f in (preprocess.html_preprocess_rules, preprocess.pdftohtml_rules, preprocess.book_designer_rules):
        f.cache_clear()  # The patterns they return must be compiled again while they are being recorded
    log = QuietLog()
    calls = []
    for doc in documents:
        for marker in ('', PDFTOHTML_MARKER, ABBYY_MARKER):
            text = marker + doc
            calls.append(lambda t=text: heuristic_processor(**ALL_HEURISTICS)(t))
            calls.append(lambda t=text: heuristic_processor(markup_chapter_headings=True, delete_blank_paragraphs=True, enable_heuristics=True)(t))
            calls.append(lambda t=text: heuristic_processor(unwrap_lines=True, html_unwrap_factor=0.1)(t))
            calls.append(lambda t=text: heuristic_processor().markup_pre(t))
            calls.append(lambda t=text: heuristic_processor().abbyy_processor(t))
            for blanks in (False, True):
                calls.append(lambda t=text, b=blanks: heuristic_processor().markup_chapters(PADDING + t, 100, b))
            calls.append(lambda t=text: HTMLPreProcessor(log, make_options(**ALL_HEURISTICS, unwrap_factor=0.4, smarten_punctuation=True))(t))
            calls.append(lambda t=text: HTMLPreProcessor(log, make_options())(t))
        calls.append(lambda t=doc: HTMLPreProcessor(log, make_options(unwrap_factor=0.4))(PDFTOHTML_MARKER + '<body>\n' + t))
        calls.append(lambda t=doc: HTMLPreProcessor(log, make_options())('<meta name="Publisher" content="Baen">' + t))
        calls.append(lambda t=doc: HTMLPreProcessor(log, make_options())('<H2 id=BookTitle>x</H2>' + t))
        for fmt in ('html', 'pdf', 'txt', 'individual_words', 'html_cleanup', 'txt_cleanup'):
            calls.append(lambda t=doc, f=fmt: Dehyphenator(0, log)(t, f, 10))
        for fmt in ('html', 'pdf', 'spanned_html', 'txt'):
            calls.append(lambda t=doc, f=fmt: DocAnalysis(f, t).line_length(0.4))
        calls.append(lambda t=doc: CSSPreProcessor()('p { mso-foo: bar; color: red }\n' + t, add_namespace=True))
        for name in (
            'clean_txt', 'convert_basic', 'separate_paragraphs_single_line', 'separate_paragraphs_print_formatted', 'separate_hard_scene_breaks',
            'block_to_single_line', 'preserve_spaces', 'remove_indents', 'detect_paragraph_type', 'detect_formatting_type',
        ):  # fmt: skip
            calls.append(lambda t=doc, n=name: getattr(txt, n)(t))
        calls.append(lambda t=doc: txt.split_txt(t, 1))
        calls.append(lambda t=doc: txt.convert_markdown(t))
        calls.append(lambda t=doc: txt.convert_textile(t))
        calls.append(lambda t=doc: txt.get_images_from_polyglot_text(t, os.getcwd(), 'txt'))
        for remove_spacing in (False, True):
            calls.append(lambda t=doc, r=remove_spacing: _record_txtml(TXTMLizer, log, t, r))
        calls.append(lambda t=doc: _record_mdml(MarkdownMLizer, log, t))
        calls.append(lambda t=doc: _record_textileml(TextileMLizer, log, t))
    for call in calls:
        try:
            call()
        except Exception:
            pass  # for example the book designer rules fail on this input. What matters is the patterns that got compiled
    return len(calls)


def _record_txtml(cls, log, text, remove_spacing):
    z = cls(log)
    z.opts = make_options(remove_paragraph_spacing=remove_spacing)
    z.cleanup_text(text)
    z.remove_newlines(text)


def _record_mdml(cls, log, text):
    z = cls(log)
    z.remove_space_after_newline = True
    z.tidy_up(text)
    z.remove_newlines(text)
    z.prepare_string_for_markdown(text)


def _record_textileml(cls, log, text):
    z = cls(log)
    z.opts = make_options(keep_links=True)
    z.our_links = ['#a', '#b']
    z.our_ids = ['#a', '#c']
    z.remove_space_after_newline = True
    z.tidy_up(text)
    z.remove_newlines(text)
    z.prepare_string_for_textile(text)


def collect_patterns():
    """All the regular expressions of the conversion heuristics and TXT processors as {(pattern, flags): {(file, function, line)}}"""
    ans = defaultdict(set)
    for d in conversion_source_dirs():
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if x != '__pycache__']
            for fname in sorted(filenames):
                if fname.endswith('.py'):
                    for pattern, flags, rel, func, lineno in static_patterns(os.path.join(dirpath, fname)):
                        ans[normalize_pattern(pattern), flags].add((rel, func, lineno))
    with PatternRecorder() as recorder:
        run_everything_once()
    for pattern, flags, rel, func, lineno in recorder.patterns:
        ans[normalize_pattern(pattern), flags].add((rel, func, lineno))
    return ans


def find_offenders(lines=None):
    """Returns {(file, function, rule): {pattern: description}}. If lines is a dict it is filled with {(file, function, rule): {line numbers}}"""
    ans = defaultdict(dict)
    for (pattern, flags), places in collect_patterns().items():
        try:
            findings = lint_pattern(pattern, flags)
        except re.error:
            continue
        rules = {}
        for rule, detail in findings:
            rules.setdefault(rule, detail)
        for rel, func, lineno in places:
            for rule, detail in rules.items():
                ans[rel, func, rule][pattern] = detail
                if lines is not None:
                    lines.setdefault((rel, func, rule), set()).add(lineno)
    return ans


# ----------------------------------------------------------------------------
# Timing tests
# ----------------------------------------------------------------------------


def _call(**options):
    def run(text):
        # The heuristics are skipped for documents with less than 50 words
        return heuristic_processor(**options)(PADDING + text)

    return run


def _dehyphenate(fmt):
    def run(text):
        from calibre.ebooks.conversion.preprocess import Dehyphenator

        # The lookbehind of the patterns needs some text before the match
        return Dehyphenator(0, QuietLog())('x' * 12 + ' ' + text, fmt, 10)

    return run


def _doc_analysis(fmt):
    def run(text):
        from calibre.ebooks.conversion.preprocess import DocAnalysis

        d = DocAnalysis(fmt, text)
        d.line_length(0.4)
        d.line_histogram(0.5)

    return run


def _preprocess(prefix='', **options):
    def run(text):
        from calibre.ebooks.conversion.preprocess import HTMLPreProcessor

        return HTMLPreProcessor(QuietLog(), make_options(**options))(prefix + text)

    return run


def _txt(name, *args):
    def run(text):
        from calibre.ebooks.txt import processor

        return getattr(processor, name)(text, *args)

    return run


def _method(name):
    def run(text):
        return getattr(heuristic_processor(), name)(text)

    return run


ENTRY_POINTS = {
    'HeuristicProcessor(all heuristics)': _call(**ALL_HEURISTICS),
    'HeuristicProcessor(chapter headings)': _call(markup_chapter_headings=True, enable_heuristics=True),
    'HeuristicProcessor(scene breaks)': _call(format_scene_breaks=True, enable_heuristics=True),
    'HeuristicProcessor(delete blank paragraphs)': _call(delete_blank_paragraphs=True, enable_heuristics=True),
    'HeuristicProcessor(unwrap lines)': _call(unwrap_lines=True, enable_heuristics=True),
    'HeuristicProcessor(dehyphenate)': _call(dehyphenate=True, enable_heuristics=True),
    'HeuristicProcessor(italicize)': _call(italicize_common_cases=True, enable_heuristics=True),
    'HeuristicProcessor(fix indents)': _call(fix_indents=True, enable_heuristics=True),
    'HeuristicProcessor(renumber headings)': _call(renumber_headings=True, enable_heuristics=True),
    'HeuristicProcessor.detect_soft_breaks': _method('detect_soft_breaks'),
    'HeuristicProcessor.detect_scene_breaks': _method('detect_scene_breaks'),
    'HeuristicProcessor.markup_pre': _method('markup_pre'),
    'HeuristicProcessor.cleanup_markup': _method('cleanup_markup'),
    'Dehyphenator(html)': _dehyphenate('html'),
    'Dehyphenator(pdf)': _dehyphenate('pdf'),
    'Dehyphenator(txt)': _dehyphenate('txt'),
    'Dehyphenator(individual_words)': _dehyphenate('individual_words'),
    'Dehyphenator(html_cleanup)': _dehyphenate('html_cleanup'),
    'Dehyphenator(txt_cleanup)': _dehyphenate('txt_cleanup'),
    'DocAnalysis(html)': _doc_analysis('html'),
    'DocAnalysis(txt)': _doc_analysis('txt'),
    'HTMLPreProcessor': _preprocess(),
    'HTMLPreProcessor(pdftohtml)': _preprocess(PDFTOHTML_MARKER + '<body>\n', unwrap_factor=0.4),
    'txt.clean_txt': _txt('clean_txt'),
    'txt.convert_basic': _txt('convert_basic'),
    'txt.detect_paragraph_type': _txt('detect_paragraph_type'),
    'txt.detect_formatting_type': _txt('detect_formatting_type'),
    'txt.separate_paragraphs_print_formatted': _txt('separate_paragraphs_print_formatted'),
    'txt.separate_hard_scene_breaks': _txt('separate_hard_scene_breaks'),
    'txt.preserve_spaces': _txt('preserve_spaces'),
    'txt.remove_indents': _txt('remove_indents'),
    'txt.block_to_single_line': _txt('block_to_single_line'),
}  # fmt: skip


def _txtml(remove_paragraph_spacing):
    def run(text):
        from calibre.ebooks.txt.txtml import TXTMLizer

        z = TXTMLizer(QuietLog())
        z.opts = make_options(remove_paragraph_spacing=remove_paragraph_spacing)
        z.cleanup_text(text)
        z.remove_newlines(text)

    return run


def _markdownml(text):
    from calibre.ebooks.txt.markdownml import MarkdownMLizer

    z = MarkdownMLizer(QuietLog())
    z.remove_space_after_newline = True
    z.tidy_up(text)
    z.remove_newlines(text)


def _textileml(text):
    from calibre.ebooks.txt.textileml import TextileMLizer

    z = TextileMLizer(QuietLog())
    z.opts = make_options(keep_links=True)
    z.our_links, z.our_ids = ['#a', '#b'], ['#a', '#c']
    z.remove_space_after_newline = True
    z.tidy_up(text)
    z.remove_newlines(text)


def _all_txt_processors(text):
    from calibre.ebooks.txt import processor

    for name in ('clean_txt', 'convert_basic', 'detect_paragraph_type', 'detect_formatting_type', 'separate_paragraphs_print_formatted',
                 'separate_hard_scene_breaks', 'block_to_single_line', 'preserve_spaces', 'remove_indents'):  # fmt: skip
        getattr(processor, name)(text)
    _txtml(False)(text)
    _txtml(True)(text)
    _markdownml(text)
    _textileml(text)


def _all_dehyphenators(text):
    for fmt in ('html', 'pdf', 'txt', 'individual_words', 'html_cleanup', 'txt_cleanup'):
        _dehyphenate(fmt)(text)


ENTRY_POINTS.update({
    'txtml.cleanup_text': _txtml(False),
    'txtml.cleanup_text(remove paragraph spacing)': _txtml(True),
    'MarkdownMLizer.tidy_up': _markdownml,
    'TextileMLizer.tidy_up': _textileml,
    'all TXT processors': _all_txt_processors,
    'all Dehyphenator formats': _all_dehyphenators,
    'txt.convert_textile': _txt('convert_textile'),
    'txt.convert_markdown': _txt('convert_markdown'),
})  # fmt: skip


def realistic_book(paragraphs, html=True):
    """A book of the given number of paragraphs with some chapter headings, scene breaks and blank paragraphs"""
    import random

    rng = random.Random(7)
    words = ('the of and a to in he was that it his her you as had with for she not at but be my on have him is said me which by so this all '
             'from they no were if would or when what there been one could very an who them').split()  # fmt: skip
    out = []
    for i in range(paragraphs):
        text = ' '.join(rng.choice(words) for _ in range(rng.randint(5, 60))).capitalize() + rng.choice(['.', '.', ',', '', '!', '-'])
        if html:
            if i % 50 == 0:
                out.append(f'<p class="calibre1"><b>CHAPTER {i // 50 + 1}</b></p>\n<p>Title words here</p>\n')
            if rng.random() < 0.05:
                out.append('<p>* * *</p>\n')
            elif rng.random() < 0.1:
                out.append('<p> </p>\n')
            out.append(f'<p class="calibre1"><span class="c2">{text}</span></p>\n')
            if rng.random() < 0.3:
                out.append('<div class="mbp_pagebreak"> </div>\n')
        else:
            out.append(('    ' if i % 3 else '') + text + '.\n' + ('\n' if i % 2 else ''))
    return '<html><body>\n' + ''.join(out) + '</body></html>' if html else ''.join(out)


# Text with the shape that makes the engine backtrack, as a function of its size n
FAMILIES = {
    # A line padded with whitespace on both sides, that is not closed. The two cases from the soft/scene break fix.
    'padded_line': lambda n: '<p><span>' + ' ' * n + 'text' + ' ' * n + '</u>',
    'padded_scene_break': lambda n: '<p><i>' + ' ' * n + '* * *' + ' ' * n + '</u>',
    'hyphenated_lines_html': lambda n: 'word-</span></p>\n<p>next ' * n,
    'hyphenated_lines_txt': lambda n: 'word-\n    next ' * n,
    'spaces': lambda n: ' ' * n,
    'newlines': lambda n: '\n' * n,
    'one_long_word': lambda n: 'x' * n,
    'one_long_line': lambda n: 'x ' * (n // 2),
    'tabs_and_dashes': lambda n: ('\t- - -\n' + ' ' * 20 + '\n') * (n // 30),
    'many_paragraphs': lambda n: '<p>Some text, with punctuation. Some more text!</p>\n' * n,
    'unclosed_tags': lambda n: '<p><b><span>' * n,
    'realistic_book': realistic_book,
    'realistic_text': lambda n: realistic_book(n, html=False),
    # Shapes that make some of the patterns slow
    'blank_paragraph_then_blank_divs': lambda n: '<p></p>' + '<div> </div> ' * n,
    'hash_run': lambda n: '<p>' + '#' * n + '!</p>',
    'long_chapter_title': lambda n: '<p>CHAPTER 1</p>\n<p>' + 'a' * n + '!</p>',
    'emphasized_padding': lambda n: '<p><span><i><b>' + ' ' * n,
    'blank_paragraphs': lambda n: '<p></p>' * n,
    'nested_open_tags': lambda n: '<p><span><i>' * n,
    'unclosed_pre': lambda n: '<pre>' * n,
    'padded_hyphen_between_tags': lambda n: 'word-' + ' ' * n + '</p>' + ' ' * n + '<p>' + ' ' * n,
    'padded_closing_tags': lambda n: 'word-' + ' ' * n + '</i>' + ' ' * n + '<i>' + ' ' * n,
    'blank_p_div_whitespace': lambda n: '<p>  </p>   <div> </div> ' * n,
    'long_unclosed_paragraph': lambda n: '<p>' + 'x' * n,
    'textile_images': lambda n: '!a! ' * n,
    # Shapes aimed at patterns that the lint flags but that cannot be made slow this way
    'br_star_run': lambda n: '<br>' + '* ' * n + 'x',
    'accent_spaces': lambda n: 'a\u00b4' + ' ' * n + '<br>' + ' ' * n,
}  # fmt: skip


Case = namedtuple('Case', 'entry_point family n')

# A case passes if the entry point uses at most this much CPU time on its input
CPU_LIMIT = 0.75
# and is killed after this many seconds of wall clock time, however long that takes to run
HARD_TIMEOUT = 2.5


def _child(conn, entry_point, text):
    try:
        start = time.process_time()
        ENTRY_POINTS[entry_point](text)
        conn.send(('ok', time.process_time() - start))
    except BaseException as e:  # a crash is a failure, not a test error, as the parent has to report it
        conn.send(('error', f'{type(e).__name__}: {e}'))
    finally:
        conn.close()


def run_cases(cases, workers=None, timeout=HARD_TIMEOUT):
    """Run each case in a forked child process and return {case: ('ok', cpu_seconds) | ('timeout', None) | ('error', message)}
    At most workers (default 4) children run at the same time. The cases that are killed only cost wall clock time, so
    running more children than there are CPUs makes the suite faster, and the CPU time of a child does not depend on how many run."""
    from multiprocessing.connection import wait

    ctx = multiprocessing.get_context('fork')
    workers = workers or 4
    pending, running, results = list(cases), {}, {}
    while pending or running:
        while pending and len(running) < workers:
            case = pending.pop(0)
            parent_conn, child_conn = ctx.Pipe(duplex=False)
            proc = ctx.Process(target=_child, args=(child_conn, case.entry_point, FAMILIES[case.family](case.n)))
            proc.start()
            child_conn.close()
            running[parent_conn] = (case, proc, time.monotonic() + timeout)
        ready = wait(list(running), timeout=0.05)
        now = time.monotonic()
        for conn in list(running):
            case, proc, deadline = running[conn]
            if conn in ready:
                try:
                    results[case] = conn.recv()
                except EOFError:
                    results[case] = ('error', 'the child process died')
            elif now > deadline:
                results[case] = ('timeout', None)
                proc.kill()
            else:
                continue
            conn.close()
            proc.join()
            del running[conn]
    return results


def warm_up():
    """Import everything and compile the cached regular expressions in the parent so the children start warm and CPU times only measure the matching"""
    for entry_point in ENTRY_POINTS.values():
        try:
            entry_point(PADDING * 3)
        except Exception:
            pass


TXT_ENTRY_POINTS = (
    'txt.clean_txt', 'txt.convert_basic', 'txt.detect_formatting_type', 'txt.separate_paragraphs_print_formatted', 'txt.separate_hard_scene_breaks',
    'txt.preserve_spaces', 'txt.remove_indents', 'txt.block_to_single_line', 'txtml.cleanup_text', 'txtml.cleanup_text(remove paragraph spacing)',
    'MarkdownMLizer.tidy_up', 'TextileMLizer.tidy_up', 'txt.convert_markdown',
)  # fmt: skip

# Cases that must finish within CPU_LIMIT: (entry point, family, size). These are sizes where a linear time
# implementation takes a few milliseconds.
PASSING_CASES = (
    # The two cases fixed in 7e05c67e, and the entry points that run them
    ('HeuristicProcessor.detect_soft_breaks', 'padded_line', 800),
    ('HeuristicProcessor.detect_soft_breaks', 'padded_scene_break', 800),
    ('HeuristicProcessor.detect_scene_breaks', 'padded_line', 800),
    ('HeuristicProcessor.detect_scene_breaks', 'padded_scene_break', 800),
    ('HeuristicProcessor(all heuristics)', 'padded_line', 800),
    ('HeuristicProcessor(all heuristics)', 'padded_scene_break', 800),
    ('HeuristicProcessor(scene breaks)', 'padded_line', 800),
    ('HeuristicProcessor(scene breaks)', 'padded_scene_break', 800),
    # Other long runs of whitespace or odd characters
    ('HeuristicProcessor.detect_soft_breaks', 'newlines', 3000),
    ('HeuristicProcessor.detect_soft_breaks', 'spaces', 3000),
    ('HeuristicProcessor.detect_scene_breaks', 'spaces', 3000),
    ('HeuristicProcessor(all heuristics)', 'newlines', 3000),
    ('HeuristicProcessor(all heuristics)', 'tabs_and_dashes', 5000),
    ('HeuristicProcessor(all heuristics)', 'one_long_line', 5000),
    ('HeuristicProcessor(all heuristics)', 'many_paragraphs', 500),
    ('HeuristicProcessor(chapter headings)', 'many_paragraphs', 1000),
    ('HeuristicProcessor(chapter headings)', 'spaces', 2000),
    ('HeuristicProcessor(chapter headings)', 'unclosed_tags', 400),
    ('HeuristicProcessor(delete blank paragraphs)', 'blank_paragraphs', 800),
    # Fixed multi_blank, any_multi_blank: exponential in the number of blank <div>s, 5 s for 24 of them
    ('HeuristicProcessor(delete blank paragraphs)', 'blank_paragraph_then_blank_divs', 32),
    ('HeuristicProcessor(delete blank paragraphs)', 'blank_paragraph_then_blank_divs', 5000),
    ('HeuristicProcessor(delete blank paragraphs)', 'blank_p_div_whitespace', 1000),
    ('HeuristicProcessor(all heuristics)', 'blank_paragraph_then_blank_divs', 32),
    ('HeuristicProcessor(delete blank paragraphs)', 'newlines', 3000),
    ('HeuristicProcessor(unwrap lines)', 'many_paragraphs', 1000),
    ('HeuristicProcessor(italicize)', 'many_paragraphs', 1000),
    ('HeuristicProcessor(fix indents)', 'many_paragraphs', 1000),
    ('HeuristicProcessor(renumber headings)', 'many_paragraphs', 1000),
    ('HeuristicProcessor(dehyphenate)', 'many_paragraphs', 1000),
    ('HeuristicProcessor.markup_pre', 'spaces', 3000),
    ('HeuristicProcessor.cleanup_markup', 'newlines', 3000),
    ('HTMLPreProcessor', 'padded_line', 800),
    ('HTMLPreProcessor', 'spaces', 3000),
    ('HTMLPreProcessor', 'many_paragraphs', 1000),
    ('HTMLPreProcessor(pdftohtml)', 'br_star_run', 800),
    ('HTMLPreProcessor(pdftohtml)', 'newlines', 3000),
    ('HTMLPreProcessor(pdftohtml)', 'accent_spaces', 800),
    ('Dehyphenator(html)', 'padded_line', 800),
    ('Dehyphenator(html)', 'many_paragraphs', 3000),
    ('all Dehyphenator formats', 'spaces', 5000),
    ('all Dehyphenator formats', 'newlines', 5000),
    ('all Dehyphenator formats', 'tabs_and_dashes', 5000),
    ('DocAnalysis(html)', 'many_paragraphs', 3000),
    ('DocAnalysis(html)', 'unclosed_tags', 800),
    ('DocAnalysis(txt)', 'newlines', 5000),
    *((entry, family, 5000) for entry in TXT_ENTRY_POINTS for family in ('spaces', 'newlines', 'tabs_and_dashes')),
    ('all TXT processors', 'many_paragraphs', 2000),
    ('all TXT processors', 'newlines', 3000),
    ('txt.detect_paragraph_type', 'newlines', 3000),
    ('txt.detect_paragraph_type', 'many_paragraphs', 2000),
)  # fmt: skip

# Cases that are too slow today, as {(entry point, family, size): what is slow}. Each runs as an expected failure.
# The text is "growth: time on this input; where". Fix the pattern and the test will report an unexpected success:
# move the case to PASSING_CASES then.
SLOW_CASES = {
    # Exponential
    ('Dehyphenator(html)', 'padded_closing_tags', 400):
        'cubic, 0.5 s for n=200, 5 s for n=400: preprocess.py:263, \\s*(?=<)(</span>)?\\s*(</[iubp]>\\s*){1,2} share whitespace',
    ('HTMLPreProcessor(pdftohtml)', 'padded_hyphen_between_tags', 400):
        'cubic, 4.3 s for n=400: preprocess.py:263 (see above), reached from HTMLPreProcessor.__call__ via the pdftohtml rules',
    ('HeuristicProcessor.cleanup_markup', 'padded_closing_tags', 1000):
        'cubic, 0.3 s for n=400, 1.5 s for n=800: utils.py:580, \\s*<i>\\s*(<i>\\s*</i>\\s*){0,2}\\s*</i> in the empty inline tag removal',
    ('HeuristicProcessor(all heuristics)', 'padded_closing_tags', 1000):
        'cubic, 2.0 s for n=800: utils.py:580 (see above), reached from HeuristicProcessor.__call__',
    ('HeuristicProcessor.markup_pre', 'unclosed_pre', 400):
        'cubic, 0.6 s for n=200, 5.1 s for n=400: utils.py:540 .*?(?<=<pre>)(?P<text>.*?)</pre> with DOTALL, quartic when the <pre> tags are not closed',
    # Quadratic
    ('HeuristicProcessor.detect_soft_breaks', 'unclosed_tags', 800):
        'quadratic, 1.6 s for n=400: utils.py:712, [^>]*> and \\s*<tag> groups restart at every unclosed tag. Cubic over whitespace was fixed in 7e05c67e, this is another shape',
    ('HeuristicProcessor.detect_scene_breaks', 'unclosed_tags', 2000):
        'quadratic, 0.8 s for n=800, 3.5 s for n=1600: utils.py:751, the same shape as detect_soft_breaks',
    ('HeuristicProcessor(scene breaks)', 'blank_paragraphs', 1000):
        'quadratic, 0.8 s for n=400, 3.6 s for n=800: utils.py:656 blanks_around_scene_breaks, (<p>\\s*</p>\\s*){1,}\\s*',
    ('HeuristicProcessor(scene breaks)', 'newlines', 1000):
        'quadratic, 0.8 s for n=400, 2.9 s for n=800: utils.py:656, a run of blank lines',
    ('DocAnalysis(txt)', 'unclosed_tags', 2000):
        'quadratic in the length of a text without line breaks, 0.7 s for n=800, 3 s for n=1600: preprocess.py:96 .*?\\n',
    ('txt.detect_paragraph_type', 'unclosed_tags', 2000):
        'quadratic: preprocess.py:96 .*?\\n through DocAnalysis',
    ('txt.convert_textile', 'unclosed_tags', 800):
        'quadratic in the length of a line, 0.8 s for n=400, 3.3 s for n=800: ebooks/textile/functions.py:485 (.+)(?:(?<!<br>)|(?<!<br />))\\n(?![#*\\s|])',
    ('Dehyphenator(individual_words)', 'hyphenated_lines_txt', 4000):
        'quadratic in the length of text without tags, 0.8 s for n=2000, 2.6 s for n=4000: preprocess.py:282, the lookahead (?![^<]*?>) scans to the next < or the end for every hyphen',
    ('Dehyphenator(txt)', 'hyphenated_lines_txt', 8000):
        'quadratic, 0.5 s for n=4000, 1.8 s for n=8000: preprocess.py:280, (?<=.{N})[^\\W\\-]+ is retried from every letter of a word',
    ('Dehyphenator(html)', 'hyphenated_lines_html', 8000):
        'quadratic, 0.6 s for n=4000, 2.3 s for n=8000: preprocess.py:263',
    ('all Dehyphenator formats', 'one_long_word', 4000):
        'quadratic in the length of a word, 0.4 s for n=2000, 1.8 s for n=4000: preprocess.py:272, :284 [^\\W\\-]+ is retried from every letter',
}  # fmt: skip


@unittest.skipUnless('fork' in multiprocessing.get_all_start_methods(), 'The timing tests need fork()')
class TimingTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        warm_up()
        cases = [Case(*c) for c in PASSING_CASES] + [Case(*c) for c in SLOW_CASES]
        cls.results = run_cases(cases)

    def check(self, case):
        status, value = self.results[Case(*case)]
        where = f'{case[0]} on {case[1]}({case[2]})'
        if status == 'timeout':
            self.fail(f'{where} was killed after {HARD_TIMEOUT} seconds')
        if status == 'error':
            self.fail(f'{where} failed: {value}')
        self.assertLessEqual(value, CPU_LIMIT, f'{where} took {value:.2f} seconds of CPU time. It should take a few milliseconds')


# A book of 3000 paragraphs. The limit is more than three times what these take on a slow machine, so it only catches a real regression
REALISTIC_CASES = (
    ('HeuristicProcessor(all heuristics)', 'realistic_book', 3000),
    ('HeuristicProcessor(all heuristics)', 'realistic_text', 3000),
    ('HeuristicProcessor(scene breaks)', 'realistic_book', 3000),
    ('HeuristicProcessor(chapter headings)', 'realistic_book', 3000),
    ('HTMLPreProcessor(pdftohtml)', 'realistic_book', 3000),
    ('all Dehyphenator formats', 'realistic_book', 3000),
    ('all TXT processors', 'realistic_text', 3000),
    ('txt.convert_textile', 'realistic_text', 3000),
)  # fmt: skip
REALISTIC_CPU_LIMIT = 8.0
REALISTIC_HARD_TIMEOUT = 20.0


@unittest.skipUnless('fork' in multiprocessing.get_all_start_methods(), 'The timing tests need fork()')
class RealisticBookTest(unittest.TestCase):
    """Ordinary books must not be slowed down by whatever is done to the regular expressions"""

    @classmethod
    def setUpClass(cls):
        warm_up()
        cls.results = run_cases([Case(*c) for c in REALISTIC_CASES], timeout=REALISTIC_HARD_TIMEOUT)

    def test_realistic_books(self):
        for case in REALISTIC_CASES:
            status, value = self.results[Case(*case)]
            where = f'{case[0]} on {case[1]}({case[2]})'
            with self.subTest(where):
                self.assertEqual(status, 'ok', f'{where}: {status} {value}')
                self.assertLessEqual(value, REALISTIC_CPU_LIMIT, f'{where} took {value:.2f} seconds of CPU time')


def _make_tests():
    def name(case):
        return re.sub(r'\W+', '_', 'test_{}_{}_{}'.format(*case)).lower()

    for case in PASSING_CASES:
        setattr(TimingTest, name(case), lambda self, case=case: self.check(case))
    for case, why in SLOW_CASES.items():
        test = unittest.expectedFailure(lambda self, case=case: self.check(case))
        test.__doc__ = why
        setattr(TimingTest, name(case), test)


_make_tests()


# Patterns that the linter flags. The key is (file, function, rule) and the value is
# (number of different offending patterns, what is wrong). Fix the pattern and delete the entry.
KNOWN_OFFENDERS = {
    ('ebooks/conversion/preprocess.py', 'Dehyphenator.__call__', 'stacked'): (1, 'line 263: html dehyphenation: [^\\W\\-]+ (-) \\s* (?=<) \\s* ... cubic, see SLOW_CASES'),
    ('ebooks/conversion/preprocess.py', 'HTMLPreProcessor.__call__', 'stacked'): (2, 'line 521, 524: accent repair and <br>\\s*<br> rules around \\s*'),
    ('ebooks/conversion/preprocess.py', 'accent_regex', 'stacked'): (2, 'line 360: accent \\s*(?:<br[^>]*>){0,1}\\s*accent, not slow on the accent_spaces input'),
    ('ebooks/conversion/preprocess.py', 'book_designer_rules', 'nested'): (2, 'line 433, 437: (align=)*(?(1)(\\w+))* between lazy [^><]*?'),
    ('ebooks/conversion/preprocess.py', 'book_designer_rules', 'stacked'): (2, 'line 433, 437: [^><]*? followed by (align=)* and [^><]*?'),
    ('ebooks/conversion/preprocess.py', 'pdftohtml_rules', 'stacked'): (1, 'line 407: <br>\\s*(([*#=] *){3,})\\s*<br>: spaces shared by \\s* and the group, not slow on the br_star_run input'),
    ('ebooks/conversion/utils.py', 'HeuristicProcessor.chapter_head', 'stacked'): (1, 'line 56: ^\\s*(.*?)\\s*$, applied to one heading, not measured'),
    ('ebooks/conversion/utils.py', 'HeuristicProcessor.cleanup_markup', 'stacked'): (2, 'line 579, 580: empty inline tag removal around \\s*'),
    ('ebooks/conversion/utils.py', 'HeuristicProcessor.detect_whitespace', 'stacked'): (4, 'line 646, 651, 656, 678: blanks_n_nopunct/blanks_between_paragraphs: (blank paragraphs\\s*){1,}\\s*'),
    ('ebooks/conversion/utils.py', 'HeuristicProcessor.fix_nbsp_indents', 'stacked'): (1, 'line 559: (<span>\\s*)+ \\s* before the nbsp run'),
    ('ebooks/conversion/utils.py', 'HeuristicProcessor.markup_pre', 'stacked'): (1, 'line 540: .*?(?<=<pre>)(?P<text>.*?)</pre>: cubic in the number of unclosed <pre> tags, see SLOW_CASES'),
    ('ebooks/conversion/utils.py', 'HeuristicProcessor.markup_user_break', 'stacked'): (1, 'line 768: .*?width(:|=)(?P<wnum>\\d+).*, applied to the scene break string the user typed, not to the book'),
    ('ebooks/conversion/utils.py', 'HeuristicProcessor.punctuation_unwrap', 'stacked'): (3, 'line 513-515: line joining: \\s* (style_close)? (\\s*</p>)? \\s*'),
    ('ebooks/txt/processor.py', 'detect_paragraph_type', 'stacked'): (2, 'line 280, 288: ^\\s*.+$ and ^(\\t|\\s{2,}).+$ with (?m): \\s* and .+ share spaces'),
}  # fmt: skip


class LintTest(unittest.TestCase):
    def test_linter_rules(self):
        def rules(pattern, flags=0):
            return sorted({rule for rule, detail in lint_pattern(pattern, flags)})

        for pattern in (
            r'\s*\s*', r'.*?\s*', r'\s*.*?', r'\s*(?:</b>\s*)?\s*', r'(?:x\s*)*\s*', r'a*(?:a|b)?a*', r'\s*(?P<x>.*?)\s*$', r'\d+[^>]*>',
            r'\s*(?=a|\s)\s*', r'(?<=a)\s*(?:<)?\s*', r'.*?(?<=<pre>)(?P<text>.*?)</pre>', r'\s*\s*+', r'x*(?i:X)*',
        ):  # fmt: skip
            self.assertEqual(rules(pattern), ['stacked'], pattern)
        for pattern in (
            r'(a+)+',
            r'(\w+\s*)*',
            r'(\s*<p>\s*)*',
            r'([a-z]+\s{0,3}){1,5}',
            r'(\s*<p[^>]*>\s*</p>(\s*<div>\s*</div>\s*)*){2,}',
            r'(?!([\W]+\s*)+)',
            r'(\s*<p[^>]*>\s*</p>(\s*<div[^>]*>\s*</div>\s*)*){2,}',
            r'(\s*+<p[^>]*>\s*+</p>(\s*<div[^>]*>\s*</div>\s*)*){2,}',
        ):
            self.assertIn('nested', rules(pattern), pattern)
        for pattern in (
            r'\s*<p>\s*', r'<p[^>]*>\s*</p>', r'\s*+\s*', r'(?>\s*)\s*', r'\s*(?!\s)\S+', r'\s*(?=<)\s*', r'(?<!\s)\s*(?<=a)\s*',
            r'[^>]*>', r'(<b>\s*)*', r'(\s*<p>)*', r'(?:<[ibu][^>]*>)*\s*', r'\w+\s*\d', r'(ab)*(ab)*', r'a{0,3}a{0,3}', r'[a-z]+|\s*',
            r'(?:\s*(?P<c>[a-z]+))??\s*x', r'x*(?i:y)*', r'(a|b)*c*',
            # A possessive repeat at the end of a repeated group takes all the characters, so the next iteration and what follows get none
            r'(\s*+<p[^>]*>\s*+</p>(\s*+<div[^>]*>\s*+</div>\s*+)*){2,}', r'(a*+)*a*', r'(?:b|a*+)*a*',
        ):  # fmt: skip
            self.assertEqual(rules(pattern), [], pattern)
        # A possessive repeat does not help when the group can end with the characters in some other way
        self.assertEqual(rules(r'(a*+(?:b|a)?)*a*'), ['stacked'])
        self.assertEqual(rules(r'(?:b+a*+)*a*'), ['nested'])
        # Characters that only differ in case are the same character with IGNORECASE
        self.assertEqual(rules(r'a*A*', re.IGNORECASE), ['stacked'])
        self.assertEqual(rules(r'a*A*'), [])
        # DOTALL changes what . matches
        self.assertEqual(rules(r'\n*.*', re.DOTALL), ['stacked'])
        self.assertEqual(rules(r'\n*.*'), [])
        self.assertEqual(rules(r'(?s:.)*\n*'), ['stacked'])

    @unittest.skipUnless(os.path.exists(os.path.join(conversion_source_dirs()[0], 'utils.py')), 'The source code is not available')
    def test_regex_lint(self):
        offenders = find_offenders()
        problems = []
        for key, patterns in sorted(offenders.items()):
            known = KNOWN_OFFENDERS.get(key)
            if known is None:
                problems.append(
                    f'New offender {key}: {len(patterns)} pattern(s) such as\n    '
                    + '\n    '.join(f'{d}: {p[:150]!r}' for p, d in sorted(patterns.items())[:3])
                )
            elif known[0] != len(patterns):
                problems.append(
                    f'{key} has {len(patterns)} offending patterns, KNOWN_OFFENDERS says {known[0]}. Update the table if this is a fix, rewrite the new patterns if it is not'
                )
        for key in sorted(set(KNOWN_OFFENDERS) - set(offenders)):
            problems.append(f'{key} is in KNOWN_OFFENDERS but is not an offender any more. Remove it')
        if problems:
            self.fail(
                'The regular expressions in the conversion heuristics and TXT processors changed. A new unbounded repeat next to another one,'
                ' or a repeated group that can start and end with one, makes a failed match take polynomial or exponential time.\n' + '\n'.join(problems)
            )


def measure(n=None, timeout=10.0):
    """Print the CPU time of all PASSING_CASES and SLOW_CASES, or of every entry point on every family at size n"""
    warm_up()
    if n is None:
        cases = [Case(*c) for c in (*PASSING_CASES, *SLOW_CASES, *REALISTIC_CASES)]
    else:
        cases = [Case(e, f, n) for e in ENTRY_POINTS for f in FAMILIES]
    for case, (status, value) in run_cases(cases, timeout=timeout).items():
        print(f'{case.entry_point:50} {case.family:35} {case.n:6} {status} {"" if value is None else f"{value:.3f}"}')


def main():
    lines = {}
    offenders = find_offenders(lines)
    total = 0
    for (rel, func, rule), patterns in sorted(offenders.items()):
        print(f'{rel} {func} {rule}: {len(patterns)} (lines {", ".join(map(str, sorted(lines[rel, func, rule])))})')
        for p, detail in sorted(patterns.items()):
            print(f'    {detail}: {p[:100]!r}')
        total += len(patterns)
    print(f'{total} offending patterns in {len(offenders)} places')


def find_tests():
    ans = unittest.defaultTestLoader.loadTestsFromTestCase(LintTest)
    ans.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TimingTest))
    ans.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RealisticBookTest))
    return ans


if __name__ == '__main__':
    main()

# License: GPLv3 Copyright: 2009, John Schember <john@nachtimwald.com>

import os

from calibre import walk, xml_replace_entities
from calibre.customize.conversion import InputFormatPlugin, OptionRecommendation
from calibre.ebooks.conversion.plugins.archive_input import archive_file_data
from calibre.utils.localization import _

MD_EXTENSIONS = {
    'abbr': _('Abbreviations'),
    'admonition': _('Support admonitions'),
    'attr_list': _('Add attribute to HTML tags'),
    'codehilite': _('Add code highlighting via Pygments'),
    'def_list': _('Definition lists'),
    'extra': _('Enables various common extensions'),
    'fenced_code': _('Alternative code block syntax'),
    'footnotes': _('Footnotes'),
    'legacy_attrs': _('Use legacy element attributes'),
    'legacy_em': _('Use legacy underscore handling for connected words'),
    'meta': _('Metadata in the document'),
    'nl2br': _('Treat newlines as hard breaks'),
    'sane_lists': _('Do not allow mixing list types'),
    'smarty': _("Use Markdown's internal smartypants parser"),
    'tables': _('Support tables'),
    'toc': _('Generate a table of contents'),
    'wikilinks': _('Wiki style links'),
}

TXTZ_FORMATTING_FOR_EXTENSION = {'md': 'markdown', 'markdown': 'markdown', 'textile': 'textile'}


def txtz_file_ext(path):
    return os.path.splitext(path)[1].lower().lstrip('.')


def txtz_formatting_for_extension(file_ext):
    return TXTZ_FORMATTING_FOR_EXTENSION.get(file_ext.lower().lstrip('.'))


class TXTInput(InputFormatPlugin):
    name = 'TXT Input'
    author = 'John Schember'
    description = _('Convert TXT files to HTML')
    file_types = {'txt', 'txtz', 'text', 'md', 'textile', 'markdown'}
    commit_name = 'txt_input'
    ui_data = {
        'md_extensions': MD_EXTENSIONS,
        'paragraph_types': {
            'auto': _('Try to auto detect paragraph type'),
            'block': _('Treat a blank line as a paragraph break'),
            'single': _('Assume every line is a paragraph'),
            'print': _('Assume every line starting with 2+ spaces or a tab starts a paragraph'),
            'unformatted': _('Most lines have hard line breaks, few/no blank lines or indents'),
            'off': _("Don't modify the paragraph structure"),
        },
        'formatting_types': {
            'auto': _('Automatically decide which formatting processor to use'),
            'plain': _('No formatting'),
            'heuristic': _('Use heuristics to determine chapter headings, italics, etc.'),
            'textile': _('Use the Textile markup language'),
            'markdown': _('Use the Markdown markup language'),
        },
    }

    options = {
        OptionRecommendation(
            name='formatting_type',
            recommended_value='auto',
            choices=list(ui_data['formatting_types']),
            help=_(
                'Formatting used within the document.\n'
                '* auto: {auto}\n'
                '* plain: {plain}\n'
                '* heuristic: {heuristic}\n'
                '* textile: {textile}\n'
                '* markdown: {markdown}\n'
                'To learn more about Markdown see {url}'
            ).format(url='https://daringfireball.net/projects/markdown/', **ui_data['formatting_types']),
        ),
        OptionRecommendation(
            name='paragraph_type',
            recommended_value='auto',
            choices=list(ui_data['paragraph_types']),
            help=_(
                'Paragraph structure to assume. The value of "off" is useful for formatted documents such as Markdown or Textile. '
                'Choices are:\n'
                '* auto: {auto}\n'
                '* block: {block}\n'
                '* single: {single}\n'
                '* print:  {print}\n'
                '* unformatted: {unformatted}\n'
                '* off: {off}'
            ).format(**ui_data['paragraph_types']),
        ),
        OptionRecommendation(
            name='preserve_spaces',
            recommended_value=False,
            help=_('Normally extra spaces are condensed into a single space. With this option all spaces will be displayed.'),
        ),
        OptionRecommendation(
            name='txt_in_remove_indents',
            recommended_value=False,
            help=_('Normally extra space at the beginning of lines is retained. With this option they will be removed.'),
        ),
        OptionRecommendation(
            name='markdown_extensions',
            recommended_value='footnotes, tables, toc',
            help=_(
                'Enable extensions to Markdown syntax. Extensions are formatting that is not part '
                'of the standard Markdown format. The extensions enabled by default: %default.\n'
                'To learn more about Markdown extensions, see {}\n'
                'This should be a comma separated list of extensions to enable:\n'
            ).format('https://python-markdown.github.io/extensions/')
            + '\n'.join(f'* {k}: {MD_EXTENSIONS[k]}' for k in sorted(MD_EXTENSIONS)),
        ),
    }

    def shift_file(self, fname, data):
        name, ext = os.path.splitext(fname)
        candidate = os.path.join(self.output_dir, fname)
        c = 0
        while os.path.exists(candidate):
            c += 1
            candidate = os.path.join(self.output_dir, f'{name}-{c}{ext}')
        ans = candidate
        with open(ans, 'wb') as f:
            f.write(data)
        return f.name

    def fix_resources(self, html, base_dir):
        from html5_parser import parse

        root = parse(html)
        changed = False
        base_dir = os.path.normcase(os.path.abspath(base_dir)) + os.sep
        for img in root.xpath('//img[@src]'):
            src = img.get('src')
            prefix = src.split(':', 1)[0].lower()
            if src and prefix not in ('file', 'http', 'https', 'ftp') and not os.path.isabs(src):
                src = os.path.abspath(os.path.join(base_dir, src))
                if os.path.normcase(src).startswith(base_dir) and os.path.isfile(src) and os.access(src, os.R_OK):
                    with open(src, 'rb') as f:
                        data = f.read()
                    f = self.shift_file(os.path.basename(src), data)
                    changed = True
                    img.set('src', os.path.basename(f))
        if changed:
            from lxml import etree

            html = etree.tostring(root, encoding='unicode')
        return html

    def convert(self, stream, options, file_ext, log, accelerators):
        from calibre.ebooks.chardet import detect
        from calibre.ebooks.conversion.preprocess import Dehyphenator, DocAnalysis
        from calibre.ebooks.txt.processor import (
            block_to_single_line,
            convert_basic,
            convert_markdown_with_metadata,
            convert_textile,
            detect_formatting_type,
            detect_paragraph_type,
            normalize_line_endings,
            preserve_spaces,
            remove_indents,
            separate_hard_scene_breaks,
            separate_paragraphs_print_formatted,
            separate_paragraphs_single_line,
        )
        from calibre.utils.zipfile import ZipFile

        self.log = log
        txt = b''
        log.debug('Reading text from file...')
        length = 0
        base_dir = self.output_dir = os.getcwd()
        cover_path = None

        # Extract content from zip archive.
        if file_ext == 'txtz':
            options.input_encoding = 'utf-8'
            zf = ZipFile(stream)
            zf.extractall('.')

            for x in walk('.'):
                ext = txtz_file_ext(x)
                if ext in {'txt', 'text', 'textile', 'md', 'markdown'}:
                    file_ext = ext
                    with open(x, 'rb') as tf:
                        txt += tf.read() + b'\n\n'
            if os.path.exists('metadata.opf'):
                from calibre.utils.xml_parse import safe_xml_fromstring

                with open('metadata.opf', 'rb') as mf:
                    raw = mf.read()
                try:
                    root = safe_xml_fromstring(raw)
                except Exception:
                    pass
                else:
                    txt_formatting = root.find('text-formatting')
                    if txt_formatting is not None and txt_formatting.text:
                        txt_formatting = txt_formatting.text.strip()
                        if txt_formatting in ('plain', 'textile', 'markdown') and options.formatting_type == 'auto':
                            log.info(f'Using metadata from TXTZ archive to set text formatting type to: {txt_formatting}')
                            options.formatting_type = txt_formatting
                            if txt_formatting != 'plain':
                                options.paragraph_type = 'off'
                    crelpath = root.find('cover-relpath-from-base')
                    if crelpath is not None and crelpath.text:
                        cover_path = crelpath.text

            if options.formatting_type == 'auto':
                formatting_type = txtz_formatting_for_extension(file_ext)
                if formatting_type:
                    options.formatting_type = formatting_type
                    options.paragraph_type = 'off'
        else:
            if getattr(stream, 'name', None):
                base_dir = os.path.dirname(stream.name)
            txt = stream.read()
            if file_ext in {'md', 'textile', 'markdown'}:
                options.formatting_type = {'md': 'markdown'}.get(file_ext, file_ext)
                log.info(f'File extension indicates particular formatting. Forcing formatting type to: {options.formatting_type}')
                options.paragraph_type = 'off'

        # Get the encoding of the document.
        if options.input_encoding:
            ienc = options.input_encoding
            log.debug(f'Using user specified input encoding of {ienc}')
        else:
            det_encoding = detect(txt[:4096])
            det_encoding, confidence = det_encoding['encoding'], det_encoding['confidence']
            if det_encoding and det_encoding.lower().replace('_', '-').strip() in (
                'gb2312',
                'chinese',
                'csiso58gb231280',
                'euc-cn',
                'euccn',
                'eucgb2312-cn',
                'gb2312-1980',
                'gb2312-80',
                'iso-ir-58',
            ):
                # Microsoft Word exports to HTML with encoding incorrectly set to
                # gb2312 instead of gbk. gbk is a superset of gb2312, anyway.
                det_encoding = 'gbk'
            ienc = det_encoding
            log.debug(f'Detected input encoding as {ienc} with a confidence of {confidence * 100}%')
        if not ienc:
            ienc = 'utf-8'
            log.debug(f'No input encoding specified and could not auto detect using {ienc}')
        # Remove BOM from start of txt as its presence can confuse markdown
        import codecs

        for bom in (
            codecs.BOM_UTF16_LE,
            codecs.BOM_UTF16_BE,
            codecs.BOM_UTF8,
            codecs.BOM_UTF32_LE,
            codecs.BOM_UTF32_BE,
        ):
            if txt.startswith(bom):
                txt = txt[len(bom) :]
                break
        txt = txt.decode(ienc, 'replace')

        # Replace entities
        txt = xml_replace_entities(txt)

        # Normalize line endings
        txt = normalize_line_endings(txt)

        # Detect formatting. This must happen before the paragraph type is
        # determined, because Markdown must not go through paragraph type
        # detection or rewriting (see below). detect_formatting_type() only
        # looks at the text, so the order does not change its result.
        if options.formatting_type == 'auto':
            options.formatting_type = detect_formatting_type(txt)
            log.debug(f'Auto detected formatting as {options.formatting_type}')

        # Determine the paragraph type of the document. Markdown has its own
        # paragraph rules: the single/print/block rewrites below double or
        # join newlines, which breaks fenced code, tables and lists, so an
        # unspecified (auto) paragraph type means off for Markdown. A
        # paragraph type given explicitly by the user is still honored.
        if options.paragraph_type == 'auto' and options.formatting_type == 'markdown':
            log.debug('Markdown formatting: not detecting or rewriting paragraph type')
            options.paragraph_type = 'off'
        elif options.paragraph_type == 'auto':
            options.paragraph_type = detect_paragraph_type(txt)
            if options.paragraph_type == 'unknown':
                log.debug('Could not reliably determine paragraph type using block')
                options.paragraph_type = 'block'
            else:
                log.debug(f'Auto detected paragraph type as {options.paragraph_type}')

        if options.formatting_type == 'heuristic':
            setattr(options, 'enable_heuristics', True)
            setattr(options, 'unwrap_lines', False)
            setattr(options, 'smarten_punctuation', True)

        # Reformat paragraphs to block formatting based on the detected type.
        # We don't check for block because the processor assumes block.
        # single and print at transformed to block for processing.
        if options.paragraph_type == 'single':
            txt = separate_paragraphs_single_line(txt)
        elif options.paragraph_type == 'print':
            txt = separate_hard_scene_breaks(txt)
            txt = separate_paragraphs_print_formatted(txt)
            txt = block_to_single_line(txt)
        elif options.paragraph_type == 'unformatted':
            from calibre.ebooks.conversion.utils import HeuristicProcessor

            # unwrap lines based on punctuation
            docanalysis = DocAnalysis('txt', txt)
            length = docanalysis.line_length(0.5)
            preprocessor = HeuristicProcessor(options, log=getattr(self, 'log', None))
            txt = preprocessor.punctuation_unwrap(length, txt, 'txt')
            txt = separate_paragraphs_single_line(txt)
        elif options.paragraph_type == 'block':
            txt = separate_hard_scene_breaks(txt)
            txt = block_to_single_line(txt)

        if getattr(options, 'enable_heuristics', False) and getattr(options, 'dehyphenate', False):
            docanalysis = DocAnalysis('txt', txt)
            if not length:
                length = docanalysis.line_length(0.5)
            dehyphenator = Dehyphenator(options.verbose, log=self.log)
            txt = dehyphenator(txt, 'txt', length)

        # User requested transformation on the text.
        if options.txt_in_remove_indents:
            txt = remove_indents(txt)

        # Preserve spaces will replace multiple spaces to a space
        # followed by the &nbsp; entity.
        if options.preserve_spaces:
            txt = preserve_spaces(txt)

        # Process the text using the appropriate text processor.
        self.shifted_files = []
        try:
            html = ''
            input_mi = None
            if options.formatting_type == 'markdown':
                log.debug('Running text through markdown conversion...')
                try:
                    input_mi, html = convert_markdown_with_metadata(txt, extensions=[x.strip() for x in options.markdown_extensions.split(',') if x.strip()])
                except RuntimeError:
                    raise ValueError(
                        'This txt file has malformed markup, it cannot be converted by calibre. See https://daringfireball.net/projects/markdown/syntax'
                    )
                html = self.fix_resources(html, base_dir)
            elif options.formatting_type == 'textile':
                log.debug('Running text through textile conversion...')
                html = convert_textile(txt)
                html = self.fix_resources(html, base_dir)
            else:
                log.debug('Running text through basic conversion...')
                flow_size = getattr(options, 'flow_size', 0)
                html = convert_basic(txt, epub_split_size_kb=flow_size)

            # Run the HTMLized text through the html processing plugin.
            from calibre.customize.ui import plugin_for_input_format

            html_input = plugin_for_input_format('html')
            for opt in html_input.options:
                setattr(options, opt.option.name, opt.recommended_value)
            options.input_encoding = 'utf-8'
            htmlfile = self.shift_file('index.html', html.encode('utf-8'))
            odi = options.debug_pipeline
            options.debug_pipeline = None
            # Generate oeb from html conversion.
            oeb = html_input.convert(open(htmlfile, 'rb'), options, 'html', log, {})
            options.debug_pipeline = odi
        finally:
            for x in self.shifted_files:
                os.remove(x)

        # Set metadata from file.
        if input_mi is None:
            from calibre.customize.ui import get_file_type_metadata

            input_mi = get_file_type_metadata(stream, file_ext)
        from calibre import guess_type
        from calibre.ebooks.oeb.transforms.metadata import meta_info_to_oeb_metadata

        meta_info_to_oeb_metadata(input_mi, oeb.metadata, log)
        self.html_postprocess_title = input_mi.title
        if cover_path:
            cover_data = archive_file_data(base_dir, cover_path)
            if cover_data is not None:
                cover_name, cdata = cover_data
                id, href = oeb.manifest.generate('cover', cover_name)
                oeb.manifest.add(id, href, guess_type(cover_name)[0], data=cdata)
                oeb.guide.add('cover', 'Cover', href)

        return oeb

    def postprocess_book(self, oeb, opts, log):
        for item in oeb.spine:
            if hasattr(item.data, 'xpath'):
                for title in item.data.xpath('//*[local-name()="title"]'):
                    if title.text == _('Unknown'):
                        title.text = self.html_postprocess_title


def find_tests():
    import re
    import unittest

    class TXTInputTest(unittest.TestCase):
        def test_txtz_extension_formatting(self):
            self.assertEqual(txtz_file_ext('book.md'), 'md')
            self.assertEqual(txtz_file_ext('book.MARKDOWN'), 'markdown')
            self.assertEqual(txtz_formatting_for_extension('.md'), 'markdown')
            self.assertEqual(txtz_formatting_for_extension('markdown'), 'markdown')
            self.assertEqual(txtz_formatting_for_extension('.textile'), 'textile')
            self.assertIsNone(txtz_formatting_for_extension('.txt'))

        def convert_to_html(self, name, text, *args):
            # Convert text via the CLI to an EPUB and return the main XHTML file and the CSS
            import tempfile
            import zipfile

            from calibre.ebooks.conversion.cli import main

            with tempfile.TemporaryDirectory() as tdir:
                src, dest = os.path.join(tdir, name), os.path.join(tdir, 'out.epub')
                with open(src, 'w', encoding='utf-8') as f:
                    f.write(text)
                import io
                from contextlib import redirect_stderr, redirect_stdout

                out = io.StringIO()
                with redirect_stdout(out), redirect_stderr(out):
                    ret = main(['ebook-convert', src, dest, '--language', 'en', '--title', 'T', '--authors', 'A', *args])
                self.assertFalse(ret, out.getvalue())
                with zipfile.ZipFile(dest) as zf:
                    names = zf.namelist()
                    html = ''.join(zf.read(n).decode('utf-8') for n in names if n.endswith('.xhtml') or n.endswith('.html'))
                    css = ''.join(zf.read(n).decode('utf-8') for n in names if n.endswith('.css'))
            return html, css

        # Ragged prose (no blank lines, varying line lengths): detect_paragraph_type() says 'single'
        RAGGED = (
            'The first line of this ragged prose is quite a bit longer than the others are here\n'
            'short\n'
            'a medium sized line of text\n'
            'x\n'
            'Another reasonably long line that goes on for a while before it finally ends here\n'
            'tiny\n'
            'and one more of medium length\n'
            'z\n'
        ) * 3
        MD_BLOCKS = (
            '\n\n# Title\n\n'
            '| h1 | h2 |\n|----|----|\n| a1 | b1 |\n| a2 | b2 |\n\n'
            '```\ndef f():\n    x = 1\n\n    return x\n```\n\n'
            '* item one\n* item two\n* item three\n\n'
        )

        def check_markdown_blocks(self, html):
            self.assertIn('<table', html)
            self.assertEqual(html.count('<tr'), 3, html)  # header + 2 rows, not 4 separate paragraphs
            self.assertNotIn('| a1', html)
            self.assertIn('<ul', html)
            self.assertEqual(html.count('<li'), 3)
            # the fence is an unrendered (fenced_code is off) inline code span but must hold the code on one run
            # with fenced_code on it is a pre/code block with the blank line kept as a single newline
            self.assertIn('x = 1', html)

        def test_markdown_skips_paragraph_type(self):
            text = self.RAGGED + self.MD_BLOCKS
            for args, name in (
                (('--formatting-type', 'markdown', '--markdown-extensions', 'tables,fenced_code'), 'book.txt'),  # given
                ((), 'book.txt'),  # auto detected (# heading)
                (('--markdown-extensions', 'tables,fenced_code'), 'book.md'),  # by extension (already worked)
            ):
                html, css = self.convert_to_html(name, text, *args)
                self.check_markdown_blocks(html)
                m = re.search(r'<pre[^>]*>(.*?)</pre>', html, re.S)
                self.assertTrue(m, (args, html))
                # exactly one newline between lines: the blank line inside the code block is kept, not doubled
                self.assertIn('x = 1\n\n', m.group(1).replace('<span>', ''), (args, m.group(1)))
                self.assertNotIn('\n\n\n', m.group(1), args)

        def test_markdown_explicit_paragraph_type_honored(self):
            # An explicitly requested paragraph type is still applied to Markdown input, as before
            html, css = self.convert_to_html(
                'book.txt', self.RAGGED + '\n# Title\n', '--formatting-type', 'markdown', '--paragraph-type', 'single')
            self.assertGreaterEqual(html.count('<p'), 20, html)

        def test_non_markdown_paragraph_type_unchanged(self):
            # Plain text still goes through paragraph type detection: every ragged line becomes its own paragraph
            html, css = self.convert_to_html('book.txt', self.RAGGED, '--formatting-type', 'plain')
            self.assertEqual(len(re.findall(r'<p[ >]', html)), 24, html)
            html, css = self.convert_to_html('book.txt', self.RAGGED)  # auto: plain
            self.assertEqual(len(re.findall(r'<p[ >]', html)), 24, html)

    return unittest.defaultTestLoader.loadTestsFromTestCase(TXTInputTest)


if __name__ == '__main__':
    from calibre.utils.run_tests import run_tests

    run_tests(find_tests)

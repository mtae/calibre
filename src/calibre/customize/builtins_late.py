# License: GPLv3 Copyright: 2008, Kovid Goyal <kovid at kovidgoyal.net>

# Builtin plugins that are only imported on demand, see builtins.late_group().
# The order of the classes in each function is the registration order.


def archive():
    from calibre.ebooks.metadata.archive import ArchiveExtract, KPFExtract

    return [ArchiveExtract, KPFExtract]


def devices():
    from calibre.devices.android.driver import ANDROID, S60, WEBOS
    from calibre.devices.binatone.driver import README
    from calibre.devices.blackberry.driver import BLACKBERRY, PLAYBOOK
    from calibre.devices.boeye.driver import BOEYE_BDX, BOEYE_BEX
    from calibre.devices.cybook.driver import CYBOOK, DIVA, MUSE, ORIZON
    from calibre.devices.eb600.driver import (
        BOOQ,
        COOL_ER,
        DBOOK,
        EB600,
        ECLICTO,
        ELONEX,
        GER2,
        INVESBOOK,
        ITALICA,
        MENTOR,
        PI2,
        POCKETBOOK301,
        POCKETBOOK360,
        POCKETBOOK360P,
        POCKETBOOK602,
        POCKETBOOK622,
        POCKETBOOK701,
        POCKETBOOK740,
        POCKETBOOKHD,
        SHINEBOOK,
        TOLINO,
    )
    from calibre.devices.edge.driver import EDGE
    from calibre.devices.eslick.driver import EBK52, ESLICK
    from calibre.devices.folder_device.driver import FOLDER_DEVICE_FOR_CONFIG
    from calibre.devices.hanlin.driver import BOOX, HANLINV3, HANLINV5, SPECTRA
    from calibre.devices.hanvon.driver import ALEX, AZBOOKA, EB511, KIBANO, LIBREAIR, N516, ODYSSEY, THEBOOK
    from calibre.devices.iliad.driver import ILIAD
    from calibre.devices.irexdr.driver import IREXDR800, IREXDR1000
    from calibre.devices.iriver.driver import IRIVER_STORY
    from calibre.devices.jetbook.driver import JETBOOK, JETBOOK_COLOR, JETBOOK_MINI, MIBUK
    from calibre.devices.kindle.driver import KINDLE, KINDLE2, KINDLE_DX, KINDLE_FIRE
    from calibre.devices.kobo.driver import KOBO, KOBOTOUCH
    from calibre.devices.misc import (
        ADAM,
        ALURATEK_COLOR,
        AVANT,
        CERVANTES,
        COBY,
        EEEREADER,
        EX124G,
        GEMEI,
        LUMIREAD,
        MOOVYBOOK,
        NEXTBOOK,
        PALMPRE,
        PDNOVEL,
        PDNOVEL_KOBO,
        POCKETBOOK626,
        SONYDPTS1,
        SWEEX,
        TREKSTOR,
        VELOCITYMICRO,
        WAYTEQ,
        WOXTER,
    )
    from calibre.devices.mtp.driver import MTP_DEVICE
    from calibre.devices.nokia.driver import E52, E71X, N770, N810
    from calibre.devices.nook.driver import NOOK, NOOK_COLOR
    from calibre.devices.nuut2.driver import NUUT2
    from calibre.devices.prs505.driver import PRS505
    from calibre.devices.prst1.driver import PRST1
    from calibre.devices.smart_device_app.driver import SMART_DEVICE_APP
    from calibre.devices.sne.driver import SNE
    from calibre.devices.teclast.driver import ARCHOS7O, IPAPYRUS, NEWSMY, PICO, SOVOS, STASH, SUNSTECH_EB700, TECLAST_K3, WEXLER
    from calibre.devices.user_defined.driver import USER_DEFINED

    # Order here matters. The first matched device is the one used.
    return [
        HANLINV3,
        HANLINV5,
        BLACKBERRY,
        PLAYBOOK,
        CYBOOK,
        ORIZON,
        MUSE,
        DIVA,
        ILIAD,
        IREXDR1000,
        IREXDR800,
        JETBOOK,
        JETBOOK_MINI,
        MIBUK,
        JETBOOK_COLOR,
        SHINEBOOK,
        POCKETBOOK360,
        POCKETBOOK301,
        POCKETBOOK602,
        POCKETBOOK701,
        POCKETBOOK360P,
        POCKETBOOK622,
        PI2,
        POCKETBOOKHD,
        POCKETBOOK740,
        KINDLE,
        KINDLE2,
        KINDLE_DX,
        KINDLE_FIRE,
        NOOK,
        NOOK_COLOR,
        PRS505,
        PRST1,
        ANDROID,
        S60,
        WEBOS,
        N770,
        E71X,
        E52,
        N810,
        COOL_ER,
        ESLICK,
        EBK52,
        NUUT2,
        IRIVER_STORY,
        GER2,
        ITALICA,
        ECLICTO,
        DBOOK,
        INVESBOOK,
        BOOX,
        BOOQ,
        EB600,
        TOLINO,
        README,
        N516,
        KIBANO,
        THEBOOK,
        LIBREAIR,
        EB511,
        ELONEX,
        TECLAST_K3,
        NEWSMY,
        PICO,
        SUNSTECH_EB700,
        ARCHOS7O,
        SOVOS,
        STASH,
        WEXLER,
        IPAPYRUS,
        EDGE,
        SNE,
        ALEX,
        ODYSSEY,
        PALMPRE,
        KOBO,
        KOBOTOUCH,
        AZBOOKA,
        FOLDER_DEVICE_FOR_CONFIG,
        AVANT,
        CERVANTES,
        MENTOR,
        SWEEX,
        PDNOVEL,
        SPECTRA,
        GEMEI,
        VELOCITYMICRO,
        PDNOVEL_KOBO,
        LUMIREAD,
        ALURATEK_COLOR,
        TREKSTOR,
        EEEREADER,
        NEXTBOOK,
        ADAM,
        MOOVYBOOK,
        COBY,
        EX124G,
        WAYTEQ,
        WOXTER,
        POCKETBOOK626,
        SONYDPTS1,
        BOEYE_BEX,
        BOEYE_BDX,
        MTP_DEVICE,
        SMART_DEVICE_APP,
        USER_DEFINED,
    ]


def ai():
    from calibre.ai.anthropic import AnthropicAI
    from calibre.ai.google import GoogleAI
    from calibre.ai.grok import GrokAI
    from calibre.ai.lm_studio import LMStudioAI
    from calibre.ai.ollama import OllamaAI
    from calibre.ai.open_router import OpenRouterAI
    from calibre.ai.openai import OpenAI
    from calibre.ai.openai_compatible import OpenAICompatible
    from calibre.ai.venice import VeniceAI

    return [OpenRouterAI, GoogleAI, AnthropicAI, OllamaAI, LMStudioAI, OpenAI, OpenAICompatible, GrokAI, VeniceAI]

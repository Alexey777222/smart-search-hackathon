"""Ручная разметка объединённого набора (queries_v2_fixed ∪ queries_v3)
по ТРЁМ исходам. Каталог — единственный источник доказательств
(поля: id, name). Движки и пороги здесь не используются.

Исходы:
  confirmed   — обязательные атрибуты запроса ПОДТВЕРЖДАЮТСЯ полями каталога;
                есть товар, полностью соответствующий запросу.
  unconfirmed — бренд/товар угадывается, но обязательный атрибут
                (архитектура x86, «своя» редакция pro/free/ultimate/workstation)
                НЕ МОЖЕТ БЫТЬ ПОДТВЕРЖДЁН полями каталога. Показываем похожий
                товар с предупреждением.
  none        — подходящего товара нет: либо товара нет вообще, либо каталог
                активно ПРОТИВОРЕЧИТ обязательному атрибуту (например, год:
                у TaskFlow Pro перечислены 2023/2024, а запрошен 2025).

Почему «противоречие» ≠ «не подтверждено»:
  год в каталоге МОДЕЛИРОВАН (есть в названиях) → запрошенный год можно
  подтвердить или опровергнуть → отсутствие = противоречие = none.
  архитектура/чужие редакции в каталоге НЕ МОДЕЛИРОВАНЫ для данного товара →
  подтвердить нельзя, но и опровергнуть нечем → unconfirmed.
"""

# query -> (outcome, acceptable_ids, evidence)
# acceptable_ids — допустимые ID через | (для none пусто).
MARKING = {
    # ── confirmed ────────────────────────────────────────────────
    "task flo pro 2024": ("confirmed", "SW-001",
        "name содержит 'TaskFlow Pro 2024': бренд+редакция(pro)+год(2024) подтверждены"),
    "task flo pro": ("confirmed", "SW-001|SW-002",
        "бренд+редакция подтверждены ('... Pro ...'); год не запрошен → оба Pro-года допустимы"),
    "byte note": ("confirmed", "SW-014|SW-015",
        "бренд 'ByteNote' подтверждён; архитектура не запрошена → обе редакции допустимы"),
    "netshiel antivirus plus": ("confirmed", "SW-011",
        "опечатка в бренде; 'Antivirus Plus' подтверждает категорию и редакцию plus"),
    "medieplay audio player": ("confirmed", "SW-019",
        "опечатка в бренде; 'Audio Player' подтверждён"),
    "byte note text editro": ("confirmed", "SW-014|SW-015",
        "опечатка в слове 'editor'; бренд+категория подтверждены"),
    "pixeldesk": ("confirmed", "SW-005|SW-006|SW-007",
        "бренд подтверждён; редакция/архитектура не запрошены → все PixelDesk допустимы"),
    "pixeldesk studio": ("confirmed", "SW-005|SW-006",
        "'PixelDesk Studio' подтверждено; portable/x86 не запрошены → обе студии допустимы"),
    "netshield antivirus": ("confirmed", "SW-011|SW-012",
        "бренд+категория подтверждены; plus/free не запрошены → обе допустимы"),
    "cloud vault backup": ("confirmed", "SW-008|SW-009",
        "раздельное написание бренда; 'CloudVault Backup' подтверждено; pro/std не запрошены"),
    "media play": ("confirmed", "SW-018|SW-019",
        "раздельное написание бренда; audio/video не запрошены → оба плеера допустимы"),
    "облако синхронизация cloudvault": ("confirmed", "SW-010",
        "латиница 'cloudvault' подтверждает бренд; 'синхронизация'→Sync подтверждён в name"),
    "облачный бэкап cloudvault pro": ("confirmed", "SW-008",
        "бренд+backup+'pro' подтверждены name 'CloudVault Backup Pro'"),
    "менеджер паролей securepass": ("confirmed", "SW-022|SW-023",
        "латиница 'securepass' подтверждает бренд; 'Password Manager' в name; business не запрошен"),
    "редактор текста byte note": ("confirmed", "SW-014|SW-015",
        "латиница 'byte note' подтверждает бренд; 'Text Editor' в name"),
    "генератор pdf reportgen": ("confirmed", "SW-025",
        "латиница 'reportgen' подтверждает бренд; 'PDF Generator' в name"),
    "облачный почтовый клиент": ("confirmed", "SW-024",
        "полностью русская формулировка → 'CloudMail Client': облако+почта+клиент подтверждены name"),
    "таскфлоу про 2024": ("confirmed", "SW-001",
        "транслит 'таскфлоу'→TaskFlow, 'про'→Pro, год 2024 подтверждены name"),
    "дата рескю рекавери": ("confirmed", "SW-020|SW-021",
        "транслит→DataRescue Recovery; server не запрошен → обе редакции допустимы"),
    "пиксельдеск студио": ("confirmed", "SW-005|SW-006",
        "транслит→PixelDesk Studio; portable/x86 не запрошены → обе студии допустимы"),
    "медиаплей аудио плеер": ("confirmed", "SW-019",
        "транслит→MediaPlay Audio Player подтверждён name"),
    "codeforge ide": ("confirmed", "SW-016|SW-017",
        "бренд+категория подтверждены; professional/student не запрошены → оба допустимы"),
    "cloudmail": ("confirmed", "SW-024",
        "бренд CloudMail подтверждён; единственный продукт бренда"),
    "пиксельдеск лайт": ("confirmed", "SW-007",
        "транслит→PixelDesk Lite; редакция 'Lite' подтверждена name"),
    "байтнот редактор": ("confirmed", "SW-014|SW-015",
        "транслит→ByteNote; 'Text Editor' подтверждён; архитектура не запрошена"),
    "облачный бэкап стандарт": ("confirmed", "SW-009",
        "backup+'стандарт'→Standard подтверждены name 'CloudVault Backup Standard'"),
    "таскфлоу энтерпрайз": ("confirmed", "SW-004",
        "транслит→TaskFlow; 'Enterprise' подтверждён name"),
    "медиаплей видео плеер": ("confirmed", "SW-018",
        "транслит→MediaPlay Video Player подтверждён name"),
    "кодфордж иде студент": ("confirmed", "SW-017",
        "транслит→CodeForge IDE; 'Student' подтверждён name"),
    "облачный бэкап без версии": ("confirmed", "SW-008|SW-009",
        "явно отказался от версии → обе редакции Backup допустимы; brand+category подтверждены"),

    # ── unconfirmed ──────────────────────────────────────────────
    "byte note text editor x86": ("unconfirmed", "SW-014",
        "бренд+категория подтверждены (SW-014 base, без арх-токена); x86 в каталоге НЕ моделируется → не подтверждается. SW-015=x64 противоречит x86 → исключён"),
    "pixeldesk studio portable x86": ("unconfirmed", "SW-006",
        "'PixelDesk Studio Portable' подтверждён (portable есть в name); x86 НЕ моделируется → не подтверждается"),
    "cloudvault backup ultimate": ("unconfirmed", "SW-008|SW-009",
        "'CloudVault Backup' подтверждён; редакция 'ultimate' отсутствует в name обоих Backup → не подтверждается (Pro/Standard — похожие, показывать с предупреждением)"),
    "datarescue recovery tool workstation": ("unconfirmed", "SW-020|SW-021",
        "'DataRescue Recovery Tool' подтверждён; 'workstation' отсутствует в name → не подтверждается"),
    "cloudvault sync pro": ("unconfirmed", "SW-010",
        "'CloudVault Sync' подтверждён; у Sync в каталоге НЕТ поля редакции → 'pro' не подтверждается. 'Pro' есть только у CloudVault Backup (SW-008) — другой товар"),
    "netshield firewall free": ("unconfirmed", "SW-013",
        "'NetShield Firewall' подтверждён; у Firewall нет редакции → 'free' не подтверждается. 'Free' есть у NetShield Antivirus (SW-012) — другой товар"),

    # ── none ─────────────────────────────────────────────────────
    "taskflow pro 2025": ("none", "",
        "год МОДЕЛИРОВАН: у TaskFlow Pro в name перечислены 2023/2024; 2025 отсутствует → каталог ПРОТИВОРЕЧИТ → нет подходящего товара"),
    "антивирус для сервера": ("none", "",
        "антивирусы (SW-011/012) имеют редакции Plus/Free, серверной редакции нет → противоречие; 'Server' есть только у DataRescue — другой товар"),
    "облачное хранилище файлов": ("none", "",
        "нет товара с категорией 'хранилище/files storage'; CloudVault — backup/sync, не storage → нет подходящего"),
    "фоторедактор с слоями": ("none", "",
        "в каталоге нет фоторедактора (PixelDesk — не заявлен как photo editor со слоями) → нет подходящего"),
    "видеоредактор для youtube": ("none", "",
        "MediaPlay — плеер, не редактор; видеоредактора нет → нет подходящего"),
    "калькулятор налогов": ("none", "",
        "калькуляторов в каталоге нет → нет подходящего"),
    "не существует продукт": ("none", "",
        "нет соответствия ни бренду, ни категории → нет подходящего"),
    "vpn tunnel client": ("none", "",
        "VPN-решений в каталоге нет (NetShield — antivirus/firewall) → нет подходящего"),
    "фото ретушь лица": ("none", "",
        "инструментов ретуши лица нет → нет подходящего"),
}


def is_flagged_unconfirmed(result: dict) -> bool:
    """True, если движок явно пометил результат как неподтверждённый."""
    return "не подтвержд" in result["method"].lower()

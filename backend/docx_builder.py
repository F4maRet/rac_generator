"""
Заполнение бланка рационализаторского предложения (шаблон Ф. № БИ-7) реальными данными.

Модуль работает не с "чистым" шаблоном-заготовкой, а напрямую с образцом,
предоставленным пользователем: он находит нужные таблицы/абзацы по их
структурной позиции (проверено вручную на template.docx) и подменяет в них
текст, стараясь максимально сохранить исходное форматирование (шрифт,
подчёркивание, курсив, выравнивание), просто переиспользуя существующие runs.
"""
import os
import re
from datetime import date

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

import text_clean

BUNDLED_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "template.docx")
# кастомный шаблон хранится в DATA_DIR (том, переживает docker compose up --build),
# а не рядом с кодом (тот запекается в образ при сборке) - именно расхождение между
# "где лежит файл, который редактирует человек" и "откуда его реально читает
# программа" и было причиной "шаблон не обновляется"
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data")
CUSTOM_TEMPLATE_PATH = os.path.abspath(os.path.join(DATA_DIR, "custom_template.docx"))


def current_template_path() -> str:
    """Пользовательский шаблон (если загружен) имеет приоритет над встроенным."""
    if os.path.exists(CUSTOM_TEMPLATE_PATH):
        return CUSTOM_TEMPLATE_PATH
    return BUNDLED_TEMPLATE_PATH


# оставлено для обратной совместимости с кодом/тестами, которые ссылались на
# модульную константу напрямую - теперь это всегда встроенный шаблон "по умолчанию"
TEMPLATE_PATH = BUNDLED_TEMPLATE_PATH

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

SECTION_HEADERS = [
    "НАЗНАЧЕНИЕ ПРЕДЛОЖЕНИЯ",
    "СВЕДЕНИЯ О НЕДОСТАТКАХ",
    "ОПИСАНИЕ ПРЕДЛАГАЕМОГО ТЕХНИЧЕСКОГО РЕШЕНИЯ",
    "ГРАФИЧЕСКОЕ ИЗОБРАЖЕНИЕ ПРЕДЛОЖЕНИЯ",
    "СТЕПЕНЬ РАЗРАБОТКИ ПРЕДЛОЖЕНИЯ",
    "ОБОСНОВАНИЕ ПОЛОЖИТЕЛЬНОГО ЭФФЕКТА",
]


APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Europe/Moscow")


def today_local() -> date:
    """Сегодняшняя дата по часовому поясу пользователей (по умолчанию -
    Москва), а не по часам сервера: контейнер Docker работает в UTC, и с 00:00
    до 03:00 по Москве в документ раньше попадала вчерашняя дата."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(APP_TIMEZONE)).date()
    except Exception:
        return date.today()


def format_date_ru(d: date) -> str:
    return f"«{d.day}» {MONTHS_RU[d.month - 1]} {d.year} г."


def initials(full_name: str) -> str:
    """"Иванов Иван Иванович" -> "Иванов И.И." """
    parts = full_name.split()
    if not parts:
        return full_name
    surname = parts[0]
    rest = parts[1:]
    inits = "".join(f"{p[0]}." for p in rest if p)
    return f"{surname} {inits}".strip()


def _set_run_range_text(paragraph, start_idx: int, end_idx: int, text: str):
    """Собирает текст в первый run диапазона [start_idx, end_idx), остальные runs очищает."""
    runs = paragraph.runs
    if start_idx >= len(runs):
        return
    runs[start_idx].text = text
    for i in range(start_idx + 1, min(end_idx, len(runs))):
        runs[i].text = ""


def _set_paragraph_text_simple(paragraph, text: str):
    runs = paragraph.runs
    if not runs:
        paragraph.add_run(text)
        return
    runs[0].text = text
    for r in runs[1:]:
        r.text = ""


def _norm(s: str) -> str:
    return s.replace("\xa0", " ")


_PERCENT_LINE_RE = re.compile(r"^\s*\d+\s*[.)]")


def re_match_percent_line(text: str) -> bool:
    """True, если абзац похож на настоящую строку распределения долей
    (начинается с номера "1." / "1)" и т.п.), а не на случайное упоминание
    слова "процентов" где-то ещё в документе - см. validate_template."""
    return bool(_PERCENT_LINE_RE.match(_norm(text)))


def _find_paragraph_index(paragraphs, needle: str, start=0):
    needle_n = _norm(needle)
    for i in range(start, len(paragraphs)):
        if needle_n in _norm(paragraphs[i].text):
            return i
    raise ValueError(f"Не найден абзац, содержащий: {needle!r}")


def _replace_date_in_paragraph(paragraph, new_date_text: str, run_span: int):
    """Заменяет дату в начале абзаца (первые run_span runs) на new_date_text."""
    _set_run_range_text(paragraph, 0, run_span, new_date_text)


def _wrap_title(title: str, max_len: int = 58):
    """Разбивает длинное наименование на 2 строки по границе слова в виде
    "пирамиды": первая (верхняя) строка не длиннее второй - визуально уже
    сверху и шире снизу, как и предполагает оформление бланка.

    Раньше строки заполнялись жадно ("пока влезает - добавляем в первую"),
    из-за чего первая строка почти всегда получалась ДЛИННЕЕ второй -
    перевёрнутая пирамида, а не обычная. Теперь перебираем все возможные
    границы разбиения по словам и выбираем ту, где первая строка не длиннее
    второй и разница между ними минимальна (наиболее сбалансированный вид,
    без переворота)."""
    title = title.strip()
    if len(title) <= max_len:
        return title, ""
    words = title.split(" ")
    if len(words) < 2:
        return title, ""  # одно длинное слово - делить не на чем

    best = None
    for k in range(1, len(words)):
        line1 = " ".join(words[:k])
        line2 = " ".join(words[k:])
        if len(line1) <= len(line2):
            diff = len(line2) - len(line1)
            if best is None or diff < best[0]:
                best = (diff, line1, line2)
    if best is None:
        # ни одно разбиение не даёт первую строку короче второй (например,
        # очень длинное последнее слово) - делим пополам по числу слов
        k = len(words) // 2 or 1
        return " ".join(words[:k]), " ".join(words[k:])
    return best[1], best[2]


def _fill_title_table(table, title: str):
    line1, line2 = _wrap_title(title)
    _set_paragraph_text_simple(table.rows[0].cells[0].paragraphs[0], line1)
    _set_paragraph_text_simple(table.rows[1].cells[0].paragraphs[0], line2)


def _replace_placeholder_in_paragraph(paragraph, placeholder: str, value: str) -> bool:
    """Заменяет placeholder внутри абзаца, сохраняя форматирование окружающего
    текста. Сначала пробует "быстрый путь" - весь placeholder лежит в одном
    run (так обычно и есть). Если Word разбил плейсхолдер на несколько runs
    (типично при ручном наборе фигурных скобок), ищет МИНИМАЛЬНЫЙ непрерывный
    диапазон runs, полностью покрывающий placeholder, и схлопывает только его -
    не трогая ничего за его пределами.

    Важно искать именно минимальный диапазон, а не "первое подходящее
    накопление от начала абзаца": в блоке {{VICE_HEAD}} ... {{VICE_FIO}}
    оба плейсхолдера разбиты на несколько runs, и наивный поиск "растущей
    суммы текста от run 0" находит {{VICE_FIO}} только после того, как
    accumulator дорастёт до конца абзаца - и тогда схлопывает ВСЕ runs
    от начала до конца в один, стирая курсив/подчёркивание у "НИЦ", табов
    и самого ФИО (реальный баг, который тут и был)."""
    runs = paragraph.runs
    for run in runs:
        if placeholder in run.text:
            run.text = run.text.replace(placeholder, value)
            return True

    texts = [r.text for r in runs]
    full_text = "".join(texts)
    idx = full_text.find(placeholder)
    if idx == -1:
        return False
    end_idx = idx + len(placeholder)

    pos = 0
    start_run = end_run = None
    for i, t in enumerate(texts):
        run_end = pos + len(t)
        if start_run is None and run_end > idx:
            start_run = i
        if run_end >= end_idx:
            end_run = i
            break
        pos = run_end
    if start_run is None or end_run is None:
        return False

    covered = "".join(texts[start_run:end_run + 1])
    local_idx = covered.find(placeholder)
    runs[start_run].text = covered[:local_idx] + value + covered[local_idx + len(placeholder):]
    for k in range(start_run + 1, end_run + 1):
        runs[k].text = ""
    return True


def _fill_signature_table(table, author_names: list):
    """Блок подписей теперь размечен плейсхолдером {{SIGN}} прямо в шаблоне
    (таблица уже расширена под обычные ФИО) - подстановка сводится к простой
    точечной замене текста, без пересчёта ширины. Очень длинные фамилии здесь
    осознанно не обрабатываются отдельно (по договорённости)."""
    for i, row in enumerate(table.rows):
        p = row.cells[0].paragraphs[0]
        name = initials(author_names[i]) if i < len(author_names) else ""
        if not _replace_placeholder_in_paragraph(p, "{{SIGN}}", name):
            # запасной вариант, если вдруг в ячейке нет плейсхолдера (старый шаблон)
            runs = p.runs
            if runs:
                runs[0].text = name
                for r in runs[1:]:
                    r.text = ""


def _insert_body_paragraphs(doc: Document, header_text: str, next_header_text_or_none, new_text: str):
    """
    Находит абзац-заголовок раздела (header_text), удаляет все абзацы тела раздела
    до следующего заголовка (next_header_text_or_none), и вставляет новые абзацы
    (по параграфам new_text, разделённым \\n\\n) с ЖЁСТКО заданным форматированием:
    Times New Roman 10pt, выключка по ширине, отступ первой строки 1,25 см,
    одинарный межстрочный интервал — как того требует ГОСТ-образец, независимо
    от того, какими (не всегда одинаковыми) табуляциями это было сделано в
    исходном примере.
    """
    paragraphs = doc.paragraphs
    header_idx = _find_paragraph_index(paragraphs, header_text)
    header_p = paragraphs[header_idx]

    # собираем все абзацы тела раздела: от header_idx+1 до next_header (не включая)
    body_ps = []
    i = header_idx + 1
    while i < len(paragraphs):
        p = paragraphs[i]
        if next_header_text_or_none and _norm(next_header_text_or_none) in _norm(p.text):
            break
        body_ps.append(p)
        i += 1

    # оставляем один пустой абзац-разделитель в конце раздела (перед следующим заголовком),
    # если он был в оригинале - для сохранения визуального интервала между разделами
    keep_last_empty = bool(body_ps) and body_ps[-1].text.strip() == "" and len(body_ps) > 1
    to_remove = body_ps[:-1] if keep_last_empty else body_ps
    for p in to_remove:
        p._p.getparent().remove(p._p)

    # Абзац = непустая строка: раньше делили только по пустой строке, и текст,
    # где абзацы разделены ОДНИМ переводом строки, превращался в один абзац с
    # принудительными разрывами (Word растягивал такие строки по ширине, красная
    # строка была только у первого). Для текста с пустыми строками между
    # абзацами результат не меняется (см. text_clean.body_paragraphs).
    new_paragraphs_text = text_clean.body_paragraphs(new_text)
    if not new_paragraphs_text:
        new_paragraphs_text = [text_clean.single_line(new_text).strip()]

    cursor = header_p._p
    for para_text in new_paragraphs_text:
        new_para = doc.add_paragraph()
        new_p_el = new_para._p
        cursor.addnext(new_p_el)
        cursor = new_p_el

        pf = new_para.paragraph_format
        pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pf.first_line_indent = Cm(1.25)
        pf.line_spacing_rule = WD_LINE_SPACING.SINGLE
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)

        run = new_para.add_run(para_text)
        run.font.size = Pt(10)
        rPr = run._r.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rPr.insert(0, rFonts)
        for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            rFonts.set(qn(attr), "Times New Roman")


REQUIRED_TABLE_SHAPES = [(7, 4), (2, 1), (6, 1), (2, 1), (6, 1)]
REQUIRED_PLACEHOLDERS = ["{{SIGN}}", "{{VICE_HEAD}}", "{{VICE_FIO}}"] + [f"{{{{section{i}}}}}" for i in range(1, 7)]


def _all_text(doc: Document) -> str:
    parts = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
    return "\n".join(parts)


def validate_template(path: str) -> tuple:
    """
    Строгая проверка загружаемого шаблона, в два этапа:

    1. Явные структурные проверки - число и форма таблиц (авторы/название x2/
       подписи x2), наличие всех плейсхолдеров и заголовков разделов. Это
       обязательный шаг: некоторые части генератора при отсутствии плейсхолдера
       не падают с ошибкой, а тихо переключаются на упрощённый запасной вариант
       (например блок подписей без невидимой точки-якоря) - такой шаблон
       технически "соберётся", но результат будет хуже ожидаемого, поэтому
       для ЗАГРУЗКИ (в отличие от обычной генерации) это считается отказом.
    2. Пробная сборка документа тестовыми данными во временный файл - ловит
       всё остальное (сломанные даты, съехавшие индексы абзацев и т.п.).

    Возвращает (ok: bool, message: str).
    """
    import tempfile

    try:
        doc = Document(path)
    except Exception as e:
        return False, f"Файл не открывается как .docx: {e}"

    shapes = [(len(t.rows), len(t.columns)) for t in doc.tables]
    if shapes != REQUIRED_TABLE_SHAPES:
        return False, (
            f"Не совпадает структура таблиц бланка. Ожидались таблицы формы "
            f"{REQUIRED_TABLE_SHAPES} (авторы, название x2, подписи x2), "
            f"а в файле: {shapes}."
        )

    full_text = _norm(_all_text(doc))
    missing_placeholders = [ph for ph in REQUIRED_PLACEHOLDERS if ph not in full_text]
    if missing_placeholders:
        return False, f"В шаблоне не хватает плейсхолдеров: {', '.join(missing_placeholders)}."

    missing_headers = [h for h in SECTION_HEADERS if h not in full_text]
    if missing_headers:
        return False, f"В шаблоне не хватает заголовков разделов: {', '.join(missing_headers)}."

    if full_text.count("процентов") < 6:
        return False, "В шаблоне должно быть 6 строк с процентным распределением («... процентов»)."

    # Раньше проверялось только общее количество упоминаний слова "процентов"
    # (>= 6) - этого недостаточно: build_docx берёт ПЕРВЫЕ 6 абзацев с этим
    # словом по порядку в документе (_find_paragraph_index), и если перед
    # настоящим блоком распределения долей окажется случайное упоминание
    # (инструкция, сноска), доли авторов попадут в чужой абзац, а эта
    # проверка (как и общий счётчик) ничего не заметит. Поэтому дополнительно
    # проверяем, что именно те 6 абзацев, которые реально будет использовать
    # build_docx, похожи на настоящие строки распределения (начинаются с
    # номера "N." - как и положено строке вида "1.  Фамилия И.О.  10 процентов").
    # Законные доп. упоминания слова ПОСЛЕ этих 6 (например, подсказка-коментарий
    # под таблицей "...число процентов") не мешают - они не используются в
    # build_docx и потому не проверяются.
    first_six = []
    search_from = 0
    paragraphs = doc.paragraphs
    for _ in range(6):
        try:
            pi = _find_paragraph_index(paragraphs, "процентов", start=search_from)
        except ValueError:
            break
        first_six.append(pi)
        search_from = pi + 1
    if len(first_six) < 6:
        return False, "В шаблоне должно быть 6 строк с процентным распределением («... процентов»)."
    bad = [i for i in first_six if not re_match_percent_line(paragraphs[i].text)]
    if bad:
        return False, (
            "Первые 6 абзацев с упоминанием «процентов» в документе не похожи на строки "
            "распределения долей авторов (ожидался формат «N. Фамилия И.О. XX процентов» - "
            "с номером в начале). Проверьте, не стоит ли перед настоящим блоком распределения "
            "случайное упоминание слова «процентов» (например, в другой инструкции)."
        )

    if "мая 2026" not in full_text:
        return False, (
            "В шаблоне не найден плейсхолдер даты (««__» мая 2026 г.» на первой странице) - "
            "именно по этому тексту программа находит, куда подставлять текущую дату."
        )

    dummy_authors = [
        {"rank": "рядовой", "full_name": "Иванов Иван Иванович", "position": "Слушатель", "workplace": "ВАС"}
        for _ in range(6)
    ]
    dummy_sections = {f"section{i}": f"Проверочный текст раздела {i}." for i in range(1, 7)}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "check.docx")
            build_docx(
                output_path=out,
                title="Способ проверки шаблона",
                sections=dummy_sections,
                authors=dummy_authors,
                percentages=[10, 10, 10, 10, 40, 20],
                vice_fio="И.Иванов",
                vice_head="Врио начальника",
                template_path=path,
            )
        return True, "Шаблон совместим - все обязательные элементы на месте, пробная сборка прошла успешно."
    except Exception as e:
        return False, f"Шаблон не подходит: {e}"


def build_docx(
    output_path: str,
    title: str,
    sections: dict,
    authors: list,
    percentages: list,
    vice_fio: str,
    vice_head: str = "Врио начальника",
    proposal_date: date = None,
    template_path: str = None,
):
    """
    authors: список из 6 dict {rank, full_name, position, workplace}
    percentages: список из 6 int, сумма = 100
    sections: dict с ключами section1..section6
    vice_head: текст должности/статуса подписанта решения на стр.2 (например
               "Врио начальника" или "Начальник") - подставляется в {{VICE_HEAD}}
    vice_fio: ФИО подписанта в формате "И.Фамилия" - подставляется в {{VICE_FIO}}
    template_path: явный путь к шаблону (используется при проверке нового
                    загружаемого шаблона); по умолчанию - текущий активный
                    (пользовательский, если загружен, иначе встроенный)
    """
    template_path = template_path or current_template_path()
    if len(authors) != 6:
        raise ValueError("Нужно ровно 6 авторов (в соответствии с формой бланка)")
    if len(percentages) != 6:
        raise ValueError("Нужно ровно 6 значений процентов")
    if sum(percentages) != 100:
        raise ValueError(f"Сумма процентов должна быть равна 100, сейчас {sum(percentages)}")

    # Однострочные поля: переводы строк/табуляции -> пробел, недопустимые для
    # Word символы удаляются (раньше, например, символ из скопированного PDF
    # обрушивал сборку документа). Обычный текст не меняется.
    title = text_clean.single_line(title)
    vice_fio = text_clean.single_line(vice_fio)
    vice_head = text_clean.single_line(vice_head)
    authors = [
        {k: text_clean.single_line(a.get(k, "")) for k in ("rank", "full_name", "position", "workplace")}
        for a in authors
    ]

    proposal_date = proposal_date or today_local()
    date_text = format_date_ru(proposal_date)

    doc = Document(template_path)

    # ---- даты (стр.1 верх, стр.1 низ "Приложения", последняя страница низ) ----
    paragraphs = doc.paragraphs
    idx = _find_paragraph_index(paragraphs, "мая 2026")
    date_no_suffix = f"«{proposal_date.day}» {MONTHS_RU[proposal_date.month - 1]} {proposal_date.year}"
    _replace_date_in_paragraph(paragraphs[idx], date_no_suffix, run_span=5)  # runs 0-4 = "«__» мая 2026"; run 5 keeps " г. ..."

    # два оставшихся отдельно стоящих "«___» мая 2026 г." абзаца (стр.1 низ и последняя страница)
    date_only_indices = [i for i, p in enumerate(paragraphs) if p.text.strip() in ("«___» мая 2026 г.", "«__» мая 2026 г.")]
    for i in date_only_indices:
        _set_paragraph_text_simple(paragraphs[i], date_text)

    # ---- таблица авторов (звание, ФИО, должность, место службы) ----
    author_table = doc.tables[0]
    for row_i, author in enumerate(authors):
        cells = author_table.rows[row_i + 1].cells
        _set_paragraph_text_simple(cells[0].paragraphs[0], author.get("rank", ""))
        _set_paragraph_text_simple(cells[1].paragraphs[0], author.get("full_name", ""))
        _set_paragraph_text_simple(cells[2].paragraphs[0], author.get("position", ""))
        _set_paragraph_text_simple(cells[3].paragraphs[0], author.get("workplace", ""))

    # ---- наименование предложения (таблица на стр.1 "Способ/Модель ...") ----
    _fill_title_table(doc.tables[1], title)
    # ---- то же наименование в шапке описания на стр.3 ----
    _fill_title_table(doc.tables[3], title)

    # ---- процентное соотношение вклада (6 абзацев "N.  Фамилия И.О.   XX процентов") ----
    percent_paragraph_idxs = []
    search_from = 0
    percent_paragraphs = doc.paragraphs  # не меняется в этом цикле - кэшируем, а не дёргаем doc.paragraphs 6 раз
    for n in range(1, 7):
        pi = _find_paragraph_index(percent_paragraphs, "процентов", start=search_from)
        percent_paragraph_idxs.append(pi)
        search_from = pi + 1
    for n, pi in enumerate(percent_paragraph_idxs):
        p = percent_paragraphs[pi]
        name = initials(authors[n]["full_name"])
        runs = p.runs
        tail_idx = next((i for i, r in enumerate(runs) if "процентов" in r.text), len(runs) - 1)
        _set_run_range_text(p, 2, tail_idx, f"    {name}           {percentages[n]}")

    # ---- блок подписей на стр.1 (после "Приложения") ----
    author_names = [a["full_name"] for a in authors]
    _fill_signature_table(doc.tables[2], author_names)
    # ---- блок подписей "АВТОРЫ" в самом конце документа ----
    _fill_signature_table(doc.tables[4], author_names)

    # ---- подписант решения на стр.2: {{VICE_HEAD}} (должность/статус) + {{VICE_FIO}} (ФИО) ----
    vice_idx = _find_paragraph_index(doc.paragraphs, "{{VICE_HEAD}}")
    vice_p = doc.paragraphs[vice_idx]
    _set_run_range_text(vice_p, 0, 3, date_text + " ")  # дата в начале той же строки
    _replace_placeholder_in_paragraph(vice_p, "{{VICE_HEAD}}", vice_head)
    _replace_placeholder_in_paragraph(vice_p, "{{VICE_FIO}}", vice_fio)

    # ---- 6 разделов ОПИСАНИЯ ----
    for i, header in enumerate(SECTION_HEADERS):
        next_header = SECTION_HEADERS[i + 1] if i + 1 < len(SECTION_HEADERS) else "ДОПОЛНИТЕЛЬНЫХ ПРОВЕРОК"
        _insert_body_paragraphs(doc, header, next_header, sections[f"section{i + 1}"])

    out_dir = os.path.dirname(output_path)
    if out_dir:  # путь без папки ("doc.docx") раньше падал на os.makedirs("")
        os.makedirs(out_dir, exist_ok=True)
    doc.save(output_path)
    return output_path

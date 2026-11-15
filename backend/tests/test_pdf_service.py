import io

import fitz

from app.services.pdf_service import derive_pdf_metadata, extract_pdf


IEEE_TITLE = (
    "Computing in Integrated Terrestrial and Non-Terrestrial Networks: "
    "A Comprehensive Survey"
)


def _build_pdf(metadata_title: str | None, lines: list[tuple[str, float, float]]) -> bytes:
    document = fitz.open()
    page = document.new_page()
    for text, y, font_size in lines:
        page.insert_text((72, y), text, fontsize=font_size)
    if metadata_title is not None:
        document.set_metadata({"title": metadata_title})
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


def test_extract_pdf_ignores_ieee_publication_header_and_uses_layout_title():
    document = fitz.open()
    page = document.new_page()
    page.insert_text(
        (72, 32),
        "Date of publication 2024 00, 0000, date of current version 2024 00, 0000.",
        fontsize=8,
    )
    page.insert_text((72, 48), "IEEE ACCESS", fontsize=8)
    page.insert_textbox(fitz.Rect(72, 90, 530, 180), IEEE_TITLE, fontsize=18)
    page.insert_text((72, 210), "DOI: 10.1109/ACCESS.2024.1234567", fontsize=8)
    page.insert_text((72, 230), "Alice Smith, alice.smith@example.edu", fontsize=10)
    document.set_metadata(
        {
            "title": (
                "Date of publication 2024 00, 0000, "
                "date of current version 2024 00, 0000."
            )
        }
    )
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()

    result = extract_pdf(buffer.getvalue(), "ieee-survey.pdf")

    assert result["title"] == IEEE_TITLE
    assert result["publication_metadata"]["publication_year"] == 2024
    assert result["publication_metadata"]["doi"] == "10.1109/access.2024.1234567"


def test_extract_pdf_keeps_a_regular_paper_title():
    title = "A Practical Study of Congestion Control"
    content = _build_pdf(
        None,
        [
            ("IEEE TRANSACTIONS ON NETWORKING", 32, 8),
            (title, 100, 18),
            ("Jane Doe", 170, 10),
        ],
    )

    result = extract_pdf(content, "regular-paper.pdf")

    assert result["title"] == title


def test_extract_pdf_does_not_append_authors_or_body_to_small_layout_title():
    title = "Computer Networks Survey"
    content = _build_pdf(
        None,
        [
            (title, 72, 11),
            ("Alice Network Researcher", 88, 11),
            ("This paper studies congestion control in computer networks.", 104, 11),
        ],
    )

    result = extract_pdf(content, "networks.pdf")

    assert result["title"] == title


def test_extract_pdf_falls_back_to_filename_when_page_has_only_header_noise():
    content = _build_pdf(
        "Date of publication 2024 00, 0000.",
        [
            ("Date of publication 2024 00, 0000.", 32, 8),
            ("IEEE ACCESS", 48, 8),
            ("DOI: 10.1109/ACCESS.2024.1234567", 80, 8),
            ("alice.smith@example.edu", 100, 8),
        ],
    )

    result = extract_pdf(content, "reasonable-fallback.pdf")

    assert result["title"] == "reasonable-fallback"


def test_extract_pdf_ignores_copyright_header():
    content = _build_pdf(
        None,
        [
            ("© 2024 IEEE. Personal use is permitted.", 32, 8),
            ("A Practical Study of Congestion Control", 100, 18),
        ],
    )

    result = extract_pdf(content, "regular-paper.pdf")

    assert result["title"] == "A Practical Study of Congestion Control"


def test_extract_pdf_preserves_table_rows_and_marks_formula_text_heuristically():
    document = fitz.open()
    page = document.new_page()
    xs = [72, 220, 380]
    ys = [100, 130, 160]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]), width=1)
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y), width=1)
    for row, y in enumerate((120, 150)):
        page.insert_text((80, y), ["Method", "BGE-M3"][row], fontsize=10)
        page.insert_text((230, y), ["Score", "0.92"][row], fontsize=10)
    page.insert_text((72, 200), "x = y = z", fontsize=12)
    page.insert_text((72, 240), "Figure 1: Network architecture", fontsize=11)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()

    result = extract_pdf(buffer.getvalue(), "structured-paper.pdf")
    elements = result["pages"][0]["elements"]
    table = next(element for element in elements if element["element_type"] == "table")
    formula = next(element for element in elements if element["element_type"] == "formula")
    caption = next(
        element for element in elements if element["element_type"] == "figure_caption"
    )

    assert table["data"]["rows"] == [["Method", "Score"], ["BGE-M3", "0.92"]]
    assert table["bbox"] is not None
    assert formula["text"] == "x = y = z"
    assert formula["extraction_status"] == "heuristic_text"
    assert caption["text"].startswith("Figure 1")


def test_metadata_title_with_cjk_and_ascii_is_plausible():
    result = derive_pdf_metadata(
        {"title": "AI赋能与风险并存：大学生AI使用行为的心理效应及引导路径"},
        "",
        "fallback.pdf",
    )

    assert result["title"] == "AI赋能与风险并存：大学生AI使用行为的心理效应及引导路径"


def test_generic_template_metadata_falls_back_to_layout_title():
    title = "Network Routing for Railway Operations"
    content = _build_pdf(
        "Preparation of Papers in Two-Column Format",
        [
            (title, 90, 18),
            ("Alice Network Researcher", 170, 10),
        ],
    )

    result = extract_pdf(content, "paper-template.pdf")

    assert result["title"] == title


def test_image_elements_ignore_glyph_sized_vector_objects():
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), 0)
    pixmap.clear_with(255)
    image = pixmap.tobytes("png")
    document = fitz.open()
    page = document.new_page()
    page.insert_image(fitz.Rect(10, 10, 12, 12), stream=image)
    page.insert_image(fitz.Rect(50, 50, 90, 90), stream=image)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()

    result = extract_pdf(buffer.getvalue(), "image-filter.pdf")
    figures = [
        element
        for element in result["pages"][0]["elements"]
        if element["element_type"] == "figure"
    ]

    assert len(figures) == 1

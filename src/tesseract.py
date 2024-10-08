import os
import tempfile
import uuid

import pytesseract
from pdfixsdk.Pdfix import (
    GetPdfix,
    PdfImageParams,
    Pdfix,
    PdfMatrix,
    PdfPage,
    PdfPageRenderParams,
    kImageDIBFormatArgb,
    kImageFormatJpg,
    kPdsPageText,
    kPsTruncate,
    kRotate0,
    kSaveFull,
)
from tqdm import tqdm

import utils


class PdfixException(Exception):
    def __init__(self, message: str = "") -> None:
        self.errno = GetPdfix().GetErrorType()
        self.add_note(message if len(message) else str(GetPdfix().GetError()))


# Renders a PDF page into a temporary file, which then used for OCR
def render_pages(page: PdfPage, pdfix: Pdfix, lang: str) -> bytes:
    """Render a PDF page into a temporary file, which is then used for OCR.

    Parameters
    ----------
    page : PdfPage
        The PDF page to be processed for OCR.
    pdfix : Pdfix
        The Pdfix SDK object.
    lang : str
        The language identifier for OCR.

    Returns
    -------
    bytes
        Raw PDF bytes.

    """
    zoom = 2.0
    page_view = page.AcquirePageView(zoom, kRotate0)
    if page_view is None:
        raise PdfixException("Unable to acquire page view")

    width = page_view.GetDeviceWidth()
    height = page_view.GetDeviceHeight()
    # Create an image
    image = pdfix.CreateImage(width, height, kImageDIBFormatArgb)
    if image is None:
        raise PdfixException("Unable to create image")

    # Render page
    render_params = PdfPageRenderParams()
    render_params.image = image
    render_params.matrix = page_view.GetDeviceMatrix()
    if not page.DrawContent(render_params):
        raise PdfixException("Unable to draw content")

    # Create temp file for rendering
    with tempfile.NamedTemporaryFile() as tmp:
        # Save image to file
        stm = pdfix.CreateFileStream(tmp.name + ".jpg", kPsTruncate)
        if stm is None:
            raise PdfixException("Unable to create file stream")

        img_params = PdfImageParams()
        img_params.format = kImageFormatJpg
        img_params.quality = 100
        if not image.SaveToStream(stm, img_params):
            raise PdfixException("Unable to save image to stream")

        return pytesseract.image_to_pdf_or_hocr(
            tmp.name + ".jpg",
            extension="pdf",
            lang=lang,
        )


def ocr(
    input_path: str,
    output_path: str,
    license_name: str,
    license_key: str,
    lang: str,
) -> None:
    """Run OCR using Tesseract.

    Parameters
    ----------
    input_path : str
        Input path to the PDF file.
    output_path : str
        Output path for saving the PDF file.
    license_name : str
        Pdfix SDK license name.
    license_key : str
        Pdfix SDK license key.
    lang : str, optional
        Language identifier for OCR Tesseract. Default value: "eng".

    """
    # List of available languages
    print("Available config files: {}".format(pytesseract.get_languages(config="")))

    pdfix = GetPdfix()
    if pdfix is None:
        raise Exception("Pdfix Initialization fail")

    if license_name and license_key:
        if not pdfix.GetAccountAuthorization().Authorize(license_name, license_key):
            raise Exception("Pdfix Authorization fail")
    else:
        print("No license name or key provided. Using Pdfix trial")

    # Open doc
    doc = pdfix.OpenDoc(input_path, "")
    if doc is None:
        raise Exception("Unable to open pdf : " + str(pdfix.GetError()))

    if lang == "":
        pdf_lang = utils.translate_iso_to_tesseract(doc.GetLang())
        lang = (
            "eng" if pdf_lang is None else pdf_lang
        )  # default "eng" if pdf does not have lang identifier or is not supported

    print(f"Using langauge: {lang}")

    doc_num_pages = doc.GetNumPages()

    # Process each page
    for i in tqdm(range(doc_num_pages), desc="Processing pages"):
        page = doc.AcquirePage(i)
        if page is None:
            raise PdfixException("Unable to acquire page")

        try:
            temp_pdf = render_pages(page, pdfix, lang)
        except Exception as e:
            raise e

        temp_path = (
            tempfile.gettempdir() + str(uuid.uuid4()) + ".pdf"
        )  # temporary file for pdf generated by the OCR
        with open(temp_path, "w+b") as f:
            f.write(temp_pdf)

        temp_doc = pdfix.OpenDoc(temp_path, "")

        if temp_doc is None:
            raise Exception("Unable to open pdf : " + str(pdfix.GetError()))

        # There is always only one page in the new PDF file
        temp_page = temp_doc.AcquirePage(0)
        temp_page_box = temp_page.GetCropBox()

        # Remove other then text page objects from the page content
        temp_page_content = temp_page.GetContent()
        for j in reversed(range(temp_page_content.GetNumObjects())):
            obj = temp_page_content.GetObject(j)
            obj_type = obj.GetObjectType()
            if obj_type != kPdsPageText:
                temp_page_content.RemoveObject(obj)

        temp_page.SetContent()

        xobj = doc.CreateXObjectFromPage(temp_page)
        if xobj is None:
            raise Exception(
                "Failed to create XObject from page: " + str(pdfix.GetError()),
            )

        temp_page.Release()
        temp_doc.Close()

        os.remove(temp_path)

        crop_box = page.GetCropBox()
        rotate = page.GetRotate()

        width = crop_box.right - crop_box.left
        width_tmp = temp_page_box.right - temp_page_box.left
        height = crop_box.top - crop_box.bottom
        height_tmp = temp_page_box.top - temp_page_box.bottom

        if rotate == 90 or rotate == 270:
            width_tmp, height_tmp = height_tmp, width_tmp

        scale_x = width / width_tmp
        scale_y = height / height_tmp

        # Calculate matrix for placing xObject on a page
        rotate = (page.GetRotate() / 90) % 4
        matrix = PdfMatrix()
        matrix = utils.pdf_matrix_rotate(matrix, rotate * utils.pi / 2, False)
        matrix = utils.pdf_matrix_scale(matrix, scale_x, scale_y, False)
        if rotate == 0:
            matrix = utils.pdf_matrix_translate(
                matrix,
                crop_box.left,
                crop_box.bottom,
                False,
            )
        elif rotate == 1:
            matrix = utils.pdf_matrix_translate(
                matrix,
                crop_box.right,
                crop_box.bottom,
                False,
            )
        elif rotate == 2:
            matrix = utils.pdf_matrix_translate(
                matrix,
                crop_box.right,
                crop_box.top,
                False,
            )
        elif rotate == 3:
            matrix = utils.pdf_matrix_translate(
                matrix,
                crop_box.left,
                crop_box.top,
                False,
            )

        content = page.GetContent()
        form = content.AddNewForm(-1, xobj, matrix)
        if form is None:
            raise Exception("Failed to add xobject to page: " + str(Pdfix.GetError()))

    if not doc.Save(output_path, kSaveFull):
        raise Exception("Unable to save pdf : " + str(pdfix.GetError()))

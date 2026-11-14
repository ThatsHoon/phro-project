"""pdf: the page caps of the pdf reader.

The module holds the pdf caps only for now; the reader proper fills it in later. Callers read
the caps as `pdf.<NAME>` at call time, so a test patches this module alone.
"""
PDF_PAGE_OBJECTS = 100_000  # page objects (text, path, image, form) interpreted per page
PDF_PAGE_IMAGES = 64        # images kept per page

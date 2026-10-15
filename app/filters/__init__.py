"""File filters: parse a source file into translatable units + a skeleton, and
rebuild the translated file from the skeleton.

Importing each module runs its @register decorator, populating the filter
registry. Adding a new format is just a new module imported here."""

from app.filters import (
    angular_xmb_filter,  # noqa: F401
    apple_filter,  # noqa: F401
    apple_strings_filter,  # noqa: F401
    asciidoc_filter,  # noqa: F401
    ass_filter,  # noqa: F401
    cat_xliff,  # noqa: F401
    catkeys_filter,  # noqa: F401
    csv_filter,  # noqa: F401
    desktop_filter,  # noqa: F401
    docx_filter,  # noqa: F401
    dtd_filter,  # noqa: F401
    dxf_filter,  # noqa: F401
    email_filter,  # noqa: F401
    epub_filter,  # noqa: F401
    fluent_filter,  # noqa: F401
    html_filter,  # noqa: F401
    htmlhelp_filter,  # noqa: F401
    icml_filter,  # noqa: F401
    idml_filter,  # noqa: F401
    image_filter,  # noqa: F401
    ini_filter,  # noqa: F401
    js_module_filter,  # noqa: F401
    json5_filter,  # noqa: F401
    json_filter,  # noqa: F401
    latex_filter,  # noqa: F401
    markdown_filter,  # noqa: F401
    mdx_filter,  # noqa: F401
    mif_filter,  # noqa: F401
    mo_filter,  # noqa: F401
    odf_filter,  # noqa: F401
    pdf_filter,  # noqa: F401
    php_array_filter,  # noqa: F401
    po_filter,  # noqa: F401
    pptx_filter,  # noqa: F401
    properties_filter,  # noqa: F401
    quark_tags_filter,  # noqa: F401
    rc_filter,  # noqa: F401
    resjson_filter,  # noqa: F401
    rst_filter,  # noqa: F401
    scc_filter,  # noqa: F401
    sdl_package,  # noqa: F401
    server_page_filter,  # noqa: F401
    srt_filter,  # noqa: F401
    stf_filter,  # noqa: F401
    stl_filter,  # noqa: F401
    subtitle_text_filter,  # noqa: F401
    text_filter,  # noqa: F401
    toml_filter,  # noqa: F401
    ttml_filter,  # noqa: F401
    ttx_filter,  # noqa: F401
    vsdx_filter,  # noqa: F401
    wiki_filter,  # noqa: F401
    wordfast,  # noqa: F401
    xaml_filter,  # noqa: F401
    xcloc_filter,  # noqa: F401
    xliff_filter,  # noqa: F401
    xliff_package,  # noqa: F401
    xlsx_filter,  # noqa: F401
    xml_its,  # noqa: F401
    xml_resource,  # noqa: F401
    xslt_filter,  # noqa: F401
    yaml_filter,  # noqa: F401
)
from app.filters import base as _base
from app.filters.base import (
    Filter,  # noqa: F401
    ParseResult,  # noqa: F401
    TextUnit,  # noqa: F401
    get_filter,  # noqa: F401
    register,  # noqa: F401
    register_lazy,
    supported_exts,  # noqa: F401
    supported_formats,  # noqa: F401
)

# `.ts` is claimed twice: Qt Linguist XML (fmt "ts", when registered) and TypeScript locale
# modules (js_module_filter). Aliases are last-registration-wins, so pin the extension to the
# JS filter whatever the import order: it hands an XML `.ts` to the registered fmt "ts" filter
# (Qt Linguist) and refuses any TypeScript that is not a static message module.
_base._ALIASES["ts"] = "js"
# MadCap Flare project zips (.flprjzip): imported on first use (see base._LAZY).
register_lazy("app.filters.flare_filter", "flprjzip")

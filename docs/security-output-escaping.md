# Safe translated output

Connection Site URLs must be absolute HTTP or HTTPS URLs with a host. This applies to
web forms, API updates, local CLI commands and OAuth configuration writes. Old unsafe
Site URLs do not produce an Open link; edit the connection to supply a valid URL.

HTML downloads and connector write-back treat translated markup as text. Keep the
filter's inline placeholders to preserve source formatting. Existing escaped entities
remain escaped, and literal angle brackets are escaped before delivery. QA flags a new
literal less-than character for review.
Whole-string WordPress excerpts and HubSpot layout fields preserve matching source HTML;
new or changed markup in those translations is displayed as literal text.

Spreadsheet deliverables and data-export workbooks store translated text as string
cells. Original workbook formulas are preserved and are not translated. CSV exports
prefix text starting with equals, plus, minus, at-sign, tab or carriage return with an
apostrophe. Glossary CSV import removes that protective apostrophe. JSON and TBX exports
retain their original text representation.

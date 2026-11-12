"""Limits for data files, which come from anywhere (downloads, mail
attachments, other programs): what a label series can sensibly need, far
below what would exhaust memory. Read at call time, so tests can lower them."""

MAX_FILE_BYTES = 50 * 1024 * 1024  # the file as it is on disk
# one large XML part of an ODS or XLSX file (a sheet, the shared strings),
# unpacked: zip bombs pack gigabytes into kilobytes. The part is parsed as a
# stream and finished elements are dropped, but the text of one element is
# held whole until it ends, so this bounds the memory as well as the time.
# 2 000 000 cells in a sheet take about 100 bytes each.
MAX_MEMBER_BYTES = 200 * 1024 * 1024
# a small part that is parsed as a whole: workbook and its relations
MAX_PART_BYTES = 8 * 1024 * 1024
# elements kept at once while a large part is read: those of one row (cells,
# their values and text runs) or one shared string; everything else is
# dropped as soon as it ends
MAX_ROW_ELEMENTS = 20_000
# elements outside rows (styles, column definitions, ...): a few thousand in
# real files; parsing millions of empty ones only takes time
MAX_OTHER_ELEMENTS = 1_000_000
MAX_DEPTH = 256  # nested XML elements; spreadsheets need about 15
# all elements of one part: 2 000 000 cells take about 6 000 000; parsing
# takes about 2 seconds per million
MAX_ELEMENTS = 16_000_000
# row elements of a sheet, filled or not: as many as Excel and LibreOffice
# have rows (empty rows are dropped, but parsing them takes time)
MAX_ROW_TAGS = 1_048_576
MAX_ATTRIBUTES = 256  # of one element; spreadsheets use about 10
# distinct tag and attribute names of one part: spreadsheets use a few
# hundred, and the parser keeps each one it has seen
MAX_NAMES = 4096
# one start tag with its attributes (or a comment): the parser holds it
# whole. Conditional formats over many ranges make long ones: 80 KB in
# XLSX, 350 KB in ODS have been seen
MAX_TAG_BYTES = 2 * 1024 * 1024
# comments, CDATA sections and processing instructions of one part:
# spreadsheets have a handful
MAX_MARKUPS = 10_000
# entries of an ODS or XLSX archive: a spreadsheet has a few dozen; zipfile
# keeps an object per entry, so millions of tiny ones would fill memory
MAX_ZIP_ENTRIES = 10_000
MAX_SHEETS = 1000
MAX_STYLES = 200_000  # cell styles of an XLSX file; Excel has at most 64 000
MAX_ROWS = 100_000  # data rows, without the header row
MAX_COLUMNS = 1000
# cells of the whole table: rows times columns (each row has every column);
# also the shared strings and the cells of merged ranges
MAX_CELLS = 2_000_000
MAX_SPACES = 100  # one run of spaces in an ODS cell (<text:s text:c="..."/>)

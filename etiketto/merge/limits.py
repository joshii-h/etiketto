"""Limits for data files, which come from anywhere (downloads, mail
attachments, other programs): what a label series can sensibly need, far
below what would exhaust memory. Read at call time, so tests can lower them."""

MAX_FILE_BYTES = 50 * 1024 * 1024  # the file as it is on disk
# one XML part of an ODS or XLSX file, unpacked: zip bombs pack gigabytes into
# kilobytes; the parts are read as a stream, so this bounds time, not memory
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_ROWS = 100_000  # data rows, without the header row
MAX_COLUMNS = 1000
MAX_CELLS = 2_000_000  # filled cells of the whole table; also shared strings
MAX_SPACES = 100  # one run of spaces in an ODS cell (<text:s text:c="..."/>)

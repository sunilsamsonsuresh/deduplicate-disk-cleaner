"""Find the type of a file (photo, video, audio and so on) from its extension."""

PHOTO_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif",
    # RAW camera files. The tool finds only exact copies of these files.
    # It dates them by file name, folder name or modified time.
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf",
    ".rw2", ".raf", ".pef", ".srw", ".3fr", ".kdc", ".mrw", ".x3f",
}
# Images that Pillow can open. The tool compares the pixels of these images.
DECODABLE_IMAGE_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif",
}
VIDEO_EXT = {
    ".mp4", ".m4v", ".mov", ".qt", ".3gp", ".3g2", ".avi", ".mkv", ".wmv", ".mpg",
    ".mpeg", ".mts", ".m2ts", ".ts", ".flv", ".webm", ".vob", ".mod", ".tod", ".dv",
}
# Video files that can hold a creation date (in the 'mvhd' part of the file).
MP4_FAMILY_EXT = {".mp4", ".m4v", ".mov", ".qt", ".3gp", ".3g2", ".m4a"}
AUDIO_EXT = {
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".wma", ".aiff", ".aif",
    ".amr", ".opus", ".mid", ".midi",
}
DOCUMENT_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".rtf", ".txt", ".csv", ".pages", ".numbers", ".key", ".md", ".html", ".htm",
    ".epub", ".xps",
}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso", ".dmg"}

CATEGORY_LABEL = {
    "photos": "Photos",
    "videos": "Videos",
    "audio": "Audio",
    "documents": "Documents",
    "archives": "Archives",
    "other": "Other",
}


def category_for(ext: str) -> str:
    ext = ext.lower()
    if ext in PHOTO_EXT:
        return "photos"
    if ext in VIDEO_EXT:
        return "videos"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in DOCUMENT_EXT:
        return "documents"
    if ext in ARCHIVE_EXT:
        return "archives"
    return "other"


# Recovered files often lose their extension ("sector2538737", "FILE0001.CHK").
# For these files, the tool reads the first bytes to find the real type.
SNIFF_EXT = {"", ".chk", ".tmp", ".bin", ".dat", ".000", ".rec"}
_FTYP_BRANDS = {
    b"heic": ".heic", b"heix": ".heic", b"hevc": ".heic", b"mif1": ".heic", b"msf1": ".heic",
    b"avif": ".avif", b"qt  ": ".mov", b"M4V ": ".m4v", b"3gp4": ".3gp", b"3gp5": ".3gp", b"3g2a": ".3g2",
}


def sniff_ext(head: bytes) -> str | None:
    """Give the real extension from the first 16 bytes of a file. Give None if the type is not known."""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return ".avi"
    if head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12], ".mp4")
    if head[:4] == b"%PDF":
        return ".pdf"
    if head[:4] == b"\x1aE\xdf\xa3":
        return ".mkv"
    return None

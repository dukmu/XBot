try:
    from slugify import slugify
except Exception:
    def slugify(value, separator="-", allow_unicode=False):
        import re
        text = value if allow_unicode else value.encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[^A-Za-z0-9]+", separator, text).strip(separator).lower()
        return re.sub(re.escape(separator) + r"{2,}", separator, text)


def make_slug(title, preserve_unicode=False, separator="-"):
    return slugify(title, separator=separator)

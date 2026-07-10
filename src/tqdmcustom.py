import sys

def tqdm(iterable, total=None, desc=None):
    """Dependency-free simple stand-in for tqdm.tqdm"""
    total = total if total is not None else len(iterable)
    prefix = f"{desc}: " if desc else ""
    bar_width = 30
    for i, item in enumerate(iterable, 1):
        filled = int(bar_width * i / total)
        bar = "#" * filled + "-" * (bar_width - filled)
        print(f"\r{prefix}[{bar}] {i}/{total}", end="", file=sys.stderr, flush=True)
        yield item
    print(file=sys.stderr)

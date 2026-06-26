#!/usr/bin/env python3
"""Local image similarity search using Pillow and imagehash."""

from __future__ import annotations

import argparse
import csv
import html
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image
import imagehash


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
HASH_SIZE = 8
MAX_HASH_DISTANCE = HASH_SIZE * HASH_SIZE
ProgressCallback = Callable[[dict], None]


@dataclass(frozen=True)
class Match:
    rank: int
    similarity: float
    filename: str
    path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search a local image folder for pictures visually similar to a reference image."
    )
    parser.add_argument("--query", required=True, help="Path to the reference image.")
    parser.add_argument("--folder", required=True, help="Path to the local image folder.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=80,
        help="Minimum similarity percentage. Default: 80",
    )
    parser.add_argument(
        "--output",
        default="results",
        help="Output directory for matches.csv and preview.html. Default: results",
    )
    return parser.parse_args()


def validate_inputs(query: Path, folder: Path, threshold: float) -> None:
    if not query.is_file():
        raise ValueError(f"Reference image not found: {query}")
    if query.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported reference image type: {query.suffix}. "
            "Supported: jpg, jpeg, png, webp"
        )
    if not folder.is_dir():
        raise ValueError(f"Image folder not found: {folder}")
    if not 0 <= threshold <= 100:
        raise ValueError("--threshold must be between 0 and 100")


def iter_image_files(folder: Path) -> Iterable[Path]:
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def list_image_files(folder: Path) -> list[Path]:
    return list(iter_image_files(folder))


def image_phash(path: Path) -> imagehash.ImageHash:
    with Image.open(path) as img:
        return imagehash.phash(img.convert("RGB"), hash_size=HASH_SIZE)


def similarity_from_distance(distance: int) -> float:
    similarity = (1 - (distance / MAX_HASH_DISTANCE)) * 100
    return max(0.0, min(100.0, similarity))


def find_matches(
    query: Path,
    folder: Path,
    threshold: float,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[Match], int, int]:
    query_hash = image_phash(query)
    image_files = list_image_files(folder)
    candidates: list[tuple[float, Path]] = []
    scanned = 0
    skipped = 0

    if progress_callback:
        progress_callback(
            {
                "stage": "matching",
                "total": len(image_files),
                "scanned": scanned,
                "skipped": skipped,
                "matches": len(candidates),
                "current_file": "",
            }
        )

    for image_path in image_files:
        scanned += 1
        if progress_callback:
            progress_callback(
                {
                    "stage": "matching",
                    "total": len(image_files),
                    "scanned": scanned,
                    "skipped": skipped,
                    "matches": len(candidates),
                    "current_file": str(image_path),
                }
            )

        try:
            candidate_hash = image_phash(image_path)
        except Exception as exc:
            skipped += 1
            print(f"Skipped unreadable image: {image_path} ({exc})", file=sys.stderr)
            continue

        distance = query_hash - candidate_hash
        similarity = similarity_from_distance(distance)
        if similarity >= threshold:
            candidates.append((similarity, image_path.resolve()))
            if progress_callback:
                progress_callback(
                    {
                        "stage": "matching",
                        "total": len(image_files),
                        "scanned": scanned,
                        "skipped": skipped,
                        "matches": len(candidates),
                        "current_file": str(image_path),
                    }
                )

    candidates.sort(key=lambda item: (-item[0], str(item[1]).lower()))
    matches = [
        Match(
            rank=index,
            similarity=round(similarity, 2),
            filename=path.name,
            path=path,
        )
        for index, (similarity, path) in enumerate(candidates, start=1)
    ]
    return matches, scanned, skipped


def write_csv(matches: list[Match], output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["rank", "similarity", "filename", "path"])
        for match in matches:
            writer.writerow(
                [
                    match.rank,
                    f"{match.similarity:.2f}",
                    match.filename,
                    str(match.path),
                ]
            )


def path_to_file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def html_escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def write_html(
    matches: list[Match],
    query: Path,
    output_path: Path,
    threshold: float,
    scanned: int,
    skipped: int,
) -> None:
    query_uri = path_to_file_uri(query)
    rows = []

    for match in matches:
        image_uri = path_to_file_uri(match.path)
        rows.append(
            f"""
            <article class="card">
              <a href="{html_escape(image_uri)}" target="_blank" rel="noreferrer">
                <img src="{html_escape(image_uri)}" alt="{html_escape(match.filename)}">
              </a>
              <div class="meta">
                <strong>#{match.rank} - {match.similarity:.2f}%</strong>
                <span>{html_escape(match.filename)}</span>
                <small title="{html_escape(match.path)}">{html_escape(match.path)}</small>
              </div>
            </article>
            """
        )

    cards_html = "\n".join(rows) if rows else '<p class="empty">No matching images found.</p>'

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Image Matcher Results</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Arial, sans-serif;
      color: #1f2933;
      background: #f6f7f9;
    }}
    body {{ margin: 0; padding: 28px; }}
    header {{ max-width: 1180px; margin: 0 auto 24px; }}
    h1 {{ margin: 0 0 14px; font-size: 28px; }}
    .summary {{
      display: grid;
      grid-template-columns: 220px 1fr;
      gap: 20px;
      align-items: start;
      background: #ffffff;
      border: 1px solid #e1e5ea;
      border-radius: 8px;
      padding: 18px;
    }}
    .summary img {{
      width: 100%;
      max-height: 220px;
      object-fit: contain;
      background: #eef1f4;
      border-radius: 6px;
    }}
    .summary p {{ margin: 6px 0; line-height: 1.6; overflow-wrap: anywhere; }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: #ffffff;
      border: 1px solid #e1e5ea;
      border-radius: 8px;
      overflow: hidden;
    }}
    .card img {{
      width: 100%;
      height: 190px;
      object-fit: contain;
      display: block;
      background: #eef1f4;
    }}
    .meta {{ padding: 12px; display: grid; gap: 7px; min-width: 0; }}
    .meta strong {{ color: #0f766e; font-size: 16px; }}
    .meta span, .meta small {{ overflow-wrap: anywhere; line-height: 1.35; }}
    .meta small {{ color: #637083; font-size: 12px; }}
    .empty {{
      grid-column: 1 / -1;
      background: #ffffff;
      border: 1px solid #e1e5ea;
      border-radius: 8px;
      padding: 22px;
    }}
    @media (max-width: 700px) {{
      body {{ padding: 16px; }}
      .summary {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Image Similarity Search Results</h1>
    <section class="summary">
      <img src="{html_escape(query_uri)}" alt="Reference image">
      <div>
        <p><strong>Reference image: </strong>{html_escape(query.resolve())}</p>
        <p><strong>Threshold: </strong>{threshold:.2f}%</p>
        <p><strong>Scanned images: </strong>{scanned}</p>
        <p><strong>Matches: </strong>{len(matches)}</p>
        <p><strong>Skipped images: </strong>{skipped}</p>
      </div>
    </section>
  </header>
  <main>
    {cards_html}
  </main>
</body>
</html>
"""
    output_path.write_text(document, encoding="utf-8")


def main() -> int:
    args = parse_args()
    query = Path(args.query).expanduser().resolve()
    folder = Path(args.folder).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()

    try:
        validate_inputs(query, folder, args.threshold)
        output_dir.mkdir(parents=True, exist_ok=True)
        matches, scanned, skipped = find_matches(query, folder, args.threshold)
        csv_path = output_dir / "matches.csv"
        html_path = output_dir / "preview.html"
        write_csv(matches, csv_path)
        write_html(matches, query, html_path, args.threshold, scanned, skipped)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Image matching finished.")
    print(f"Scanned images: {scanned}")
    print(f"Matches found: {len(matches)}")
    print(f"Skipped images: {skipped}")
    print(f"CSV: {csv_path}")
    print(f"HTML preview: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

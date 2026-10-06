#!/usr/bin/env python3
"""Extract text from documents using marker-pdf. High-quality OCR + layout analysis.

Requires pre-provisioned local OCR/layout model assets; never downloads weights.
Supports: PDF, DOCX, PPTX, XLSX, HTML, EPUB, images.

Usage:
    python extract_marker.py document.pdf
    python extract_marker.py document.pdf --output_dir ./output
    python extract_marker.py presentation.pptx
    python extract_marker.py spreadsheet.xlsx
    python extract_marker.py scanned_doc.pdf           # OCR works here
    python extract_marker.py document.pdf --json        # Structured output
    python extract_marker.py document.pdf --use_llm     # LLM-boosted accuracy
"""
import sys
import os
from pathlib import Path


def _require_local_models():
    """Reject remote or incomplete model selectors before Marker starts loaders."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from surya.settings import settings

    checkpoints = [
        name for name in dir(settings) if name.endswith("_MODEL_CHECKPOINT")
    ]
    hint = (
        "Marker requires pre-provisioned local model assets. Configure every "
        "Surya *_MODEL_CHECKPOINT as a local directory containing config.json "
        "and model weights; provision local GGUF/model-projector files where "
        "the installed Surya backend uses them. Mercury never downloads weights. "
        "Use extract_pymupdf.py for ordinary local text extraction."
    )
    if not checkpoints:
        raise RuntimeError(hint + " Cannot verify this Surya version's asset selectors.")
    for name in checkpoints:
        value = getattr(settings, name, None)
        if not isinstance(value, str) or not value:
            raise RuntimeError(hint + f" Missing {name}.")
        directory = Path(value).expanduser()
        if not directory.is_dir() or not (directory / "config.json").is_file():
            raise RuntimeError(hint + f" Unavailable {name}: {value}.")
        if not any(
            file.is_file() and file.stat().st_size > 0
            for suffix in (".safetensors", ".bin", ".pth", ".gguf")
            for file in directory.glob(f"*{suffix}")
        ):
            raise RuntimeError(hint + f" No local weights for {name}: {directory}.")
        resolved = str(directory.resolve())
        setattr(settings, name, resolved)
        os.environ[name] = resolved
    for name in ("SURYA_GGUF_LOCAL_MODEL_PATH", "SURYA_GGUF_LOCAL_MMPROJ_PATH"):
        if hasattr(settings, name):
            value = getattr(settings, name)
            if not isinstance(value, str) or not value or not Path(value).expanduser().is_file():
                raise RuntimeError(hint + f" Unavailable {name}.")
            resolved = str(Path(value).expanduser().resolve())
            setattr(settings, name, resolved)
            os.environ[name] = resolved


def convert(path, output_dir=None, output_format="markdown", use_llm=False):
    _require_local_models()
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.config.parser import ConfigParser

    config_dict = {}
    if use_llm:
        config_dict["use_llm"] = True

    config_parser = ConfigParser(config_dict)
    models = create_model_dict()
    converter = PdfConverter(config=config_parser.generate_config_dict(), artifact_dict=models)
    rendered = converter(path)

    if output_format == "json":
        import json
        print(json.dumps({
            "markdown": rendered.markdown,
            "metadata": rendered.metadata if hasattr(rendered, "metadata") else {},
        }, indent=2, ensure_ascii=False))
    else:
        print(rendered.markdown)

    # Save images if output_dir specified
    if output_dir and hasattr(rendered, "images") and rendered.images:
        from pathlib import Path
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        for name, img_data in rendered.images.items():
            img_path = os.path.join(output_dir, name)
            with open(img_path, "wb") as f:
                f.write(img_data)
        print(f"\nSaved {len(rendered.images)} image(s) to {output_dir}/", file=sys.stderr)


def check_requirements():
    """Check disk space before installing."""
    import shutil
    free_gb = shutil.disk_usage("/").free / (1024**3)
    if free_gb < 5:
        print(f"⚠️  Only {free_gb:.1f}GB free. marker-pdf needs ~5GB for PyTorch + models.")
        print("Use pymupdf instead (scripts/extract_pymupdf.py) or free up disk space.")
        sys.exit(1)
    print(f"✓ {free_gb:.1f}GB free — sufficient for marker-pdf")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract text from documents using marker-pdf (high-quality OCR + layout analysis)."
    )
    parser.add_argument("path", nargs="?", help="Document to convert (PDF, DOCX, PPTX, XLSX, HTML, EPUB, image)")
    parser.add_argument("--output_dir", help="Directory to save extracted images")
    parser.add_argument("--json", action="store_true", help="Structured JSON output instead of markdown")
    parser.add_argument("--use_llm", action="store_true", help="LLM-boosted accuracy")
    parser.add_argument("--check", action="store_true", help="Check disk space requirements and exit")
    args = parser.parse_args()

    if args.check:
        check_requirements()
        sys.exit(0)
    if not args.path:
        parser.error("path is required unless --check is given")

    convert(
        args.path,
        output_dir=args.output_dir,
        output_format="json" if args.json else "markdown",
        use_llm=args.use_llm,
    )

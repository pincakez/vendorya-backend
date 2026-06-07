"""
Ingest the ERP usage catalog into AIKnowledgeChunk with Gemini embeddings.

Usage:
    python manage.py ingest_erp_catalog
    python manage.py ingest_erp_catalog --force   # re-ingest even if chunks exist
    python manage.py ingest_erp_catalog --no-embed # skip embeddings (text-only)

The source file is .claude/docs/erp-catalog.md in the monorepo root.
Splits on '## ' headings — each heading becomes one chunk.
"""
import os
import time

from django.core.management.base import BaseCommand

from admin_ai.models import AIKnowledgeChunk, AISettings
from admin_ai.services import GeminiService, GeminiError

SOURCE_NAME = "erp-catalog.md"
INDUSTRIES  = ["retail", "erp", "vendorya"]
CATALOG_PATH = os.path.join(
    os.path.dirname(__file__),
    "../../../..",        # vendorya/ root (commands → management → admin_ai → vendorya-backend → vendorya)
    ".claude/docs/erp-catalog.md",
)


def _split_catalog(text: str) -> list[tuple[int, str, str]]:
    """Split markdown by '## ' headings → list of (index, heading, content)."""
    chunks = []
    current_heading = "Introduction"
    current_lines = []
    idx = 0

    for line in text.splitlines():
        if line.startswith("## "):
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    chunks.append((idx, current_heading, body))
                    idx += 1
            current_heading = line[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            chunks.append((idx, current_heading, body))

    return chunks


class Command(BaseCommand):
    help = "Ingest the ERP catalog into the AI knowledge base."

    def add_arguments(self, parser):
        parser.add_argument("--force",    action="store_true", help="Re-ingest even if chunks already exist.")
        parser.add_argument("--no-embed", action="store_true", help="Skip embedding generation.")

    def handle(self, *args, **options):
        catalog_path = os.path.normpath(CATALOG_PATH)
        if not os.path.exists(catalog_path):
            self.stderr.write(self.style.ERROR(f"Catalog not found: {catalog_path}"))
            return

        existing = AIKnowledgeChunk.objects.filter(source_name=SOURCE_NAME, is_deleted=False).count()
        if existing and not options["force"]:
            self.stdout.write(self.style.WARNING(
                f"{existing} chunks already in KB. Use --force to re-ingest."
            ))
            return

        if options["force"] and existing:
            AIKnowledgeChunk.objects.filter(source_name=SOURCE_NAME).delete()
            self.stdout.write(f"Removed {existing} old chunks.")

        text = open(catalog_path, encoding="utf-8").read()
        sections = _split_catalog(text)
        self.stdout.write(f"Found {len(sections)} sections in catalog.")

        service = None
        if not options["no_embed"]:
            settings_obj = AISettings.load()
            if settings_obj.has_key:
                service = GeminiService(settings_obj.gemini_api_key)
                self.stdout.write("Gemini embedding enabled.")
            else:
                self.stdout.write(self.style.WARNING("No Gemini key — saving text only (no embeddings)."))

        for idx, heading, content in sections:
            chunk = AIKnowledgeChunk.objects.create(
                source_name=SOURCE_NAME,
                source_type="manual",
                chunk_index=idx,
                content=f"# {heading}\n\n{content}",
                industries=INDUSTRIES,
                metadata={"heading": heading},
            )

            if service:
                try:
                    chunk.embedding = service.embed(chunk.content)
                    chunk.save(update_fields=["embedding"])
                    self.stdout.write(f"  [{idx:02d}] {heading} — embedded ✓")
                except GeminiError as e:
                    self.stdout.write(self.style.WARNING(f"  [{idx:02d}] {heading} — embed failed: {e}"))
                time.sleep(0.3)  # stay within Gemini free-tier RPM
            else:
                self.stdout.write(f"  [{idx:02d}] {heading} — saved (no embed)")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {len(sections)} chunks ingested into KB from '{SOURCE_NAME}'."
        ))

"""Build the Tailwind stylesheet with the standalone CLI (no Node.js needed).

    python manage.py tailwind build   # minified static/css/app.css
    python manage.py tailwind watch   # rebuild on template changes while developing

The built CSS is committed, so installers and customer machines never run Tailwind.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

TAILWIND_VERSION = "v4.1.14"


class Command(BaseCommand):
    help = "Build or watch the Tailwind CSS bundle."

    def add_arguments(self, parser):
        parser.add_argument("mode", choices=["build", "watch"])

    def handle(self, *args, mode, **options):
        try:
            import pytailwindcss
        except ImportError as err:
            raise CommandError(
                "pytailwindcss is missing. Run: pip install -r requirements-dev.txt"
            ) from err

        src = settings.BASE_DIR / "static" / "src" / "app.css"
        out = settings.BASE_DIR / "static" / "css" / "app.css"
        cli_args = ["--input", str(src), "--output", str(out)]
        cli_args.append("--minify" if mode == "build" else "--watch")

        result = pytailwindcss.run(
            cli_args,
            cwd=str(settings.BASE_DIR),
            live_output=True,
            auto_install=True,
            version=TAILWIND_VERSION,
        )
        if result.returncode != 0:
            raise CommandError("Tailwind build failed.")
        if mode == "build":
            self.stdout.write(self.style.SUCCESS(f"Built {out.relative_to(settings.BASE_DIR)}"))

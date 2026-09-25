from __future__ import annotations

import datetime
import logging
import os
import tempfile

from flask import Flask, flash, redirect, render_template, request, url_for

import utils

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.secret_key = "rummy-wars-local"

LOGGER = logging.getLogger(__name__)


@app.context_processor
def inject_current_year():
    return {"current_year": datetime.datetime.now(tz=datetime.timezone.utc).date()}


def evaluate_upload(file_storage):
    """Parse and evaluate an uploaded CBS roster without writing it to disk."""
    csv_bytes = file_storage.stream.read()
    with tempfile.NamedTemporaryFile(suffix=".csv") as temporary_file:
        temporary_file.write(csv_bytes)
        temporary_file.flush()
        roster = utils.parse_cbs_roster_csv(temporary_file.name)
        violations, warnings = utils.validate_league_rules(roster)
    return roster, violations, warnings


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/evaluate")
def evaluate():
    upload = request.files.get("roster")
    if upload is None or not upload.filename:
        flash("Choose a CBS roster CSV to evaluate.", "error")
        return redirect(url_for("index"))

    if not upload.filename.lower().endswith(".csv"):
        flash("Roster exports must be CSV files.", "error")
        return redirect(url_for("index"))

    try:
        roster, violations, warnings = evaluate_upload(upload)
    except (UnicodeDecodeError, OSError, ValueError) as error:
        LOGGER.warning("Unable to process roster upload: %s", error)
        flash("That file could not be read as a CBS roster CSV.", "error")
        return redirect(url_for("index"))
    except Exception:
        LOGGER.exception("Roster evaluation failed")
        flash("The roster could not be evaluated. Check the server log for details.", "error")
        return redirect(url_for("index"))

    return render_template(
        "results.html",
        filename=upload.filename,
        roster=roster,
        violations=violations,
        warnings=warnings
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080")) # Default to Docker default
    app.run(host="127.0.0.1", port=port, debug=True)

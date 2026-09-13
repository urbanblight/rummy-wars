# rummy-wars

A tool to check compliance with roster requirements for the Rummy Wars fantasy baseball league on the CBS Fantasy Baseball platform.

## Installation
 `git clone https://github.com/urbanblight/rummy-wars/`
 
 `pip install requirements.xt`

## Usage

* Log into Rummy Wars on the CBS Fantasy Baseball platform.
* Go to the Roster page for a team and download the CSV export for that team
![Example CBS Fantasy Baseball roster page for team Ween, showing export controls.](assets/example_screenshot.png)
* `python3 -m main path/to/export.csv` 

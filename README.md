# rummy-wars

A tool to check compliance with roster requirements for the Rummy Wars fantasy baseball league on the CBS Fantasy Baseball platform.

## Installation
 `git clone https://github.com/urbanblight/rummy-wars/`
 
 `pip install requirements.xt`

## Usage

* Log into Rummy Wars on the CBS Fantasy Baseball platform.
* Go to the Roster page for a team and download the CSV export for that team
* Execute `python3 -m main path/to/export.csv` 
<p align="center">
  <img src="assets/example_screenshot.png" alt="Example CBS Fantasy Baseball roster page for team Ween, showing export controls." width="300">
</p>

## Sample Output
```
2026-09-13 14:00:40 | INFO     | main | Successfully loaded 53 players.
2026-09-13 14:00:40 | INFO     | utils | Does not exceed MiLB roster limit
2026-09-13 14:00:40 | INFO     | utils | Does not exceed IL roster limit
2026-09-13 14:00:41 | DEBUG    | utils | Ike Irish All-Time MLB AB: 0
2026-09-13 14:00:41 | DEBUG    | utils | Tre' Morgan All-Time MLB AB: 0
2026-09-13 14:00:42 | DEBUG    | utils | Dauri Fernandez All-Time MLB AB: 0
2026-09-13 14:00:42 | DEBUG    | utils | Mitch Voit All-Time MLB AB: 0
2026-09-13 14:00:43 | DEBUG    | utils | Wehiwa Aloy All-Time MLB AB: 0
2026-09-13 14:00:43 | DEBUG    | utils | Coy James All-Time MLB AB: 0
2026-09-13 14:00:44 | WARNING  | utils | Anthony Volpe is in a Minors slot but has more than 130 AB (1901).
2026-09-13 14:00:44 | DEBUG    | utils | Jaden Fauske All-Time MLB AB: 0
2026-09-13 14:00:45 | DEBUG    | utils | Nick Morabito All-Time MLB AB: 29
2026-09-13 14:00:45 | DEBUG    | utils | Paulino Santana All-Time MLB AB: 0
2026-09-13 14:00:49 | DEBUG    | utils | Junior Perez All-Time MLB AB: 35
2026-09-13 14:00:50 | DEBUG    | utils | Kevin Alvarez All-Time MLB AB: 0
2026-09-13 14:00:50 | DEBUG    | utils | Josue Briceno All-Time MLB AB: 0
2026-09-13 14:00:51 | DEBUG    | utils | Andrew Salas All-Time MLB AB: 0
2026-09-13 14:00:51 | ERROR    | utils | Unable to match CBS name 'Dorian Soto' to MLB player
2026-09-13 14:00:51 | WARNING  | utils | Unable to verify stats for Dorian Soto: none found
2026-09-13 14:00:51 | WARNING  | utils | Colby Thomas is in a Minors slot but has more than 130 AB (234).
2026-09-13 14:00:52 | WARNING  | utils | Ryan Waldschmidt is in a Minors slot but has more than 130 AB (246).
2026-09-13 14:00:52 | DEBUG    | utils | Jett Williams All-Time MLB AB: 0
2026-09-13 14:00:53 | DEBUG    | utils | Jake Bloss All-Time MLB IP: 11.2
2026-09-13 14:00:53 | WARNING  | utils | Kai-Wei Teng is in a Minors slot but has more than 50 IP (114.1).
2026-09-13 14:00:53 | INFO     | main | Roster may be compliant with evaluated rules, but check any warnings above.
```
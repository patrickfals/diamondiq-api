"""Apply human-reviewed corrections to commentary_cache_v2.json.

Every corrected level is recorded under `human_edits` with the original
model text, and the position is flagged `edited: true`, so the cache stays
honest about what IBM generated vs. what a human fixed.

Run from the diamondiq-api folder:  python apply_commentary_edits.py
"""
import json
import shutil
import sys

CACHE = "commentary_cache_v2.json"

# Full replacements: (position, level) -> corrected text
REPLACE = {
    ("1", "novice"): "Michael Soroka started the at-bat with a straight fastball that the umpire called a strike, so Trevor Story has zero balls and one strike. Three strikes is an out, so Story has two strikes left to work with. If the next pitch misses, the count evens up at one ball, one strike.",
    ("1", "learning"): "Michael Soroka opened with a four-seam fastball at 91.9 mph that was called a strike, putting Trevor Story in an 0-1 count. Starting with a strike puts the pitcher ahead. A ball next would even it at 1-1, while another strike would make it 0-2 and put Story in a real hole.",
    ("1", "expert"): "Michael Soroka opened with a 91.9 mph four-seam fastball for a called strike, putting Trevor Story at 0-1. A first-pitch strike lets the pitcher work ahead and gives him more freedom with what comes next. Story entered hitting .244 and Soroka entered with a 4.70 ERA, so getting ahead early is exactly the edge Soroka wants.",
    ("2", "expert"): "Michael Soroka delivered two consecutive four-seam fastballs to Trevor Story, first at 91.9 mph, then at 93.1 mph, both called strikes, moving the count from 0-1 to 0-2. Note the 1.2 mph jump on the same pitch: Soroka added velocity once he was ahead. Story, entering with a .244 average, now faces a pitcher holding a 4.70 ERA with two strikes in hand.",
    ("3", "learning"): "Soroka delivered two four-seam fastballs, both called strikes, pushing Story to a 0-2 count. On the third pitch, another four-seam fastball was a ball, moving the count to 1-2. Soroka is still ahead and Story still has to protect with two strikes, but the ball gives Story a little breathing room.",
    ("3", "regular"): "Michael Soroka opened with a 91.9 mph four-seam fastball that was called a strike, putting Trevor Story at 0-1. He followed with a 93.1 mph fastball, another strike, for 0-2. The third fastball, 93.6 mph, was a ball, nudging the count to 1-2. Soroka is still ahead, and Story still has to protect with two strikes.",
    ("3", "expert"): "Against Trevor Story (.244 entering the game), Michael Soroka (4.70 ERA) went fastball three straight times, and each one came in harder: 91.9, 93.1, then 93.6 mph. The first two were called strikes for 0-2; the third was a ball for 1-2. Three rising fastballs in a row says Soroka is challenging Story head-on rather than working around him.",
    ("4", "expert"): "Michael Soroka, entering with a 4.70 ERA, opened Trevor Story's at-bat with three four-seam fastballs (91.9, 93.1 and 93.6 mph), generating two called strikes and a ball for a 1-2 count. He then introduced a 79.3 mph slurve, more than 14 mph slower than the pitch before, and Story stayed alive by fouling it off, keeping the count at 1-2.",
    ("6", "expert"): "Michael Soroka, entering with a 4.70 ERA, began the top-2 at-bat versus .244-hitting Trevor Story with two four-seam fastballs, 91.9 and 93.1 mph, both called strikes for 0-2. A 93.6 mph fastball ball made it 1-2, after which Soroka's slurves, around 80 mph, produced a foul, a ball and another foul. Story has fought back from 0-2 to an even 2-2 count.",
    ("7", "novice"): "Michael Soroka opened with two fastballs called strikes, putting Trevor Story at 0-2, one strike away from a strikeout. Soroka then threw a fastball for a ball, making it 1-2, and switched to slurves that produced a foul, a ball and two more fouls. A foul with two strikes doesn't count as strike three, so it's 2-2.",
    ("7", "regular"): "Michael Soroka opened the at-bat with two four-seam fastballs, each called a strike, pushing Trevor Story into a 0-2 hole. A third fastball was called a ball, making it 1-2. Switching to slurves, Soroka got a foul, then a ball, followed by two more fouls, and Story has battled back to a 2-2 count.",
    ("7", "expert"): "Michael Soroka, whose pre-game ERA sits at 4.70, began with two four-seam fastballs (91.9 and 93.1 mph), both called strikes, leaving Trevor Story at 0-2. A 93.6 mph fastball was a ball, moving to 1-2. Soroka then went to slurves around 80 mph: a foul, a ball, and two more fouls, with the count at 2-2. Story, entering at .244, has now fouled off three two-strike slurves.",
    ("10", "learning"): "Soroka opened with two four-seam fastballs, both called strikes, putting Story down 0-2. After a ball, Soroka mixed in slurves: a foul, a ball, then two more fouls. A changeup ball ran the count full at 3-2, Story fouled off one more slurve, and the final fastball resulted in a single.",
    ("10", "regular"): "Soroka started the at-bat with two called-strike four-seam fastballs, dropping Story into a 0-2 hole. A fastball ball opened the door for a slurve barrage: foul, ball, then two fouls. A changeup ball ran the count full at 3-2, Story fouled off one more slurve, then put Soroka's final fastball in play for a single.",
    ("10", "expert"): "Soroka opened with 91.9 and 93.1 mph four-seam fastballs, both called strikes, leaving Story at 0-2. After a 93.6 mph fastball ball, Soroka went to slurves around 80 mph: foul, ball, then two fouls. An 84.8 mph changeup ball ran the count full, Story fouled off an 81.1 mph slurve, then put a 94.0 mph fastball, his hardest pitch of the at-bat, in play for a single.",
}

# Targeted substring fixes: (position, level) -> [(old, new), ...]
SUBSTITUTE = {
    ("9", "learning"): [("finishing at 3‑2 overall for the pitcher today", "and the count is now full at 3-2")],
    ("9", "regular"): [("pitches three through seven", "pitches four through seven")],
    ("9", "expert"): [("career .244 average", ".244 average entering the game"),
                      ("pitches three to seven", "pitches four to seven")],
}


def main():
    with open(CACHE, encoding="utf-8") as f:
        data = json.load(f)

    shutil.copyfile(CACHE, CACHE + ".bak")
    problems = []

    def record(pos, level, new_text):
        entry = data[pos]
        original = entry["commentary_by_level"][level]
        entry.setdefault("human_edits", {})
        # Keep the very first model text if this script is run twice.
        entry["human_edits"].setdefault(level, {"original_model_text": original})
        entry["commentary_by_level"][level] = new_text
        entry["edited"] = True

    for (pos, level), text in REPLACE.items():
        if pos not in data:
            problems.append(f"position {pos} missing")
            continue
        record(pos, level, text)

    for (pos, level), pairs in SUBSTITUTE.items():
        if pos not in data:
            problems.append(f"position {pos} missing")
            continue
        text = data[pos]["commentary_by_level"][level]
        for old, new in pairs:
            # Model output uses non-breaking hyphens (‑); try both spellings.
            candidates = [old, old.replace("-", "‑"), old.replace("‑", "-")]
            hit = next((c for c in candidates if c in text), None)
            if hit is None:
                problems.append(f"pos {pos} {level}: '{old}' not found")
                continue
            text = text.replace(hit, new)
        if text != data[pos]["commentary_by_level"][level]:
            record(pos, level, text)

    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)

    edited = sorted(((p, l) for p in data for l in data[p].get("human_edits", {})),
                    key=lambda x: int(x[0]))
    print(f"Backup written to {CACHE}.bak")
    print(f"Edited {len(edited)} commentary lines:")
    for p, l in edited:
        print(f"  position {p:>2}  {l}")
    if problems:
        print("\nPROBLEMS (not applied):")
        for msg in problems:
            print("  " + msg)
        sys.exit(1)


if __name__ == "__main__":
    main()

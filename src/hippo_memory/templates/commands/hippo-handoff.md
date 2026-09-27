---
description: Write this session's note to self for the next instance
---
Write a hand-off note with these labelled sections, each one or two lines, omitting any that
are empty: State, Decided, Dead ends, Assumed, Next, Calibration, Unsure. Save it with
`hippo handoff add --title "<one line naming the task state>" --body "$(cat <<'EOF'
<the note>
EOF
)"`, adding `--carries <id>` for each open loop it continues. Reply with the id printed.

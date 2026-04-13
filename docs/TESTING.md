# peerdup testing checklist

Test environment: Borealis (192.168.0.32, Fedora) + Thinkpad (192.168.0.20, Fedora), LAN-only shares.

---

## Install / setup

- [x] Clean install on Borealis via `curl | sh`
- [x] Clean install on Thinkpad via `curl | sh`
- [x] `peerdup identity` works on both machines
- [x] GNOME extension join share flow shows: Share ID -> local name -> folder (3 steps)

## Basic replication

- [x] Create share on Borealis, join on Thinkpad
- [x] File replication Borealis -> Thinkpad
- [x] File replication Thinkpad -> Borealis

## Directory rename replication

- [x] Rename directory on Borealis -> replicates correctly to Thinkpad (no orphan old dir)
- [x] Rename directory on Thinkpad -> replicates correctly to Borealis
- [ ] Rapid renames / multiple files (stress)

## Conflict resolution

- [ ] last_write_wins (default): newer file wins on both peers
- [ ] rename_conflict: both versions kept
- [ ] ask: share pauses, `peerdup share conflicts` shows entry, resolve works

## Multi-file operations

- [ ] Add multiple files at once - all replicate
- [ ] Delete file - replicates
- [ ] Move file within share - replicates correctly

## Edge cases

- [ ] Daemon restart mid-sync - resumes correctly
- [ ] Peer goes offline / comes back - sync resumes
- [ ] Large file transfer
- [ ] Share with many files

---

## Session notes (2026-04-13)

Clean install on both hosts. Bugs found and fixed:
- FS events silently dropped when share created with empty directory: SYNCING guard was
  too broad - now only suppresses events when `info_hash` is set (active download in
  progress). (`98760d2`)

Note: both peers independently adding files before first sync creates a last_write_wins
conflict (equal seq, different info_hash). Expected behavior - one peer's content wins.
Correct workflow: add files on one peer, let sync complete, then add files on other peer.

Stopped at: rapid rename stress test - resume here next session.

---

## Session notes (2026-04-12)

Bugs found and fixed:
- `control_pb2` bare import in CLI process (`7554237`)
- LAN announcements advertising wrong port when libtorrent increments past configured port (`ede5574`)
- Orphan directory left after remote rename: libtorrent pre-allocation blocking pre-positioning + `local_files` DB not populated on receiver (`6db50f3`)

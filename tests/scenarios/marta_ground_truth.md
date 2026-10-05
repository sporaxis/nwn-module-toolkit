# Marta module — ground truth (for regression against report.json)

Build with `python3 make_marta_module.py marta_module marta_hak`, analyse with
`python3 nwn_analyse.py marta_module --hak marta_hak --name marta`.

- Duplicates: dup_a_0/1/2 identical (Iron Sword); dup_b_0/1/2 identical (Oak Shield);
  namedup_0..4 same stats/different name; sharedtag_0..3 share tag SHARED_KEY;
  give_gold_a/b/c identical code; dlg_lost == dlg_lost_copy (planted duplicate conversation);
  sc_start_cond_a code-identical to sc_is_child_cond (both `return TRUE;` — unplanned but real).
- Missing/broken: q_missing_from_journal used by big_dlg but absent from module.jrl;
  big_dlg EntryList[12]/Script -> nonexistent script (name truncated to 16 chars by
  nwnlib's RESREF writer: "sc_does_not_exist" -> "sc_does_not_exis"); big_dlg last
  reply's EntriesList has an Index (35) beyond EntryList length (30); dlg_ghost's
  EntryList[0]/Script (sc_ghost_cond) has no main() - used as an action, wrong type.
- Case-insensitivity: item file sword_of_kings.uti has TemplateResRef "Sword_Of_Kings";
  door DOOR_3's OnOpen is "AT_GiveKey" vs file at_givekey.nss - both must resolve.
- Includes: inc_a>inc_b>inc_c chain; inc_d diamond via inc_diamond_left/right into
  inc_diamond_base; inc_cycle_x <-> inc_cycle_y mutual include cycle (used by uses_cycle);
  inc_unused_lib included by nothing; bad_include_caller calls FuncC() (defined in inc_c)
  without #including it - would fail to compile.
- Orphans: orphan_action_0..4 (main(), unreferenced); compiled_only.ncs (no .nss source).
- Dynamic: dyn_caller uses ExecuteScript("dyn_target"-in-a-variable) and
  ExecuteScript(GetLocalString(...)) (fully dynamic target).
- String-only conversation reference: summon_ghost_dlg.nss calls
  ActionStartConversation(oPC,"dlg_ghost") - dlg_ghost has no NPC Conversation field
  pointing to it anywhere else.
- Areas: area01..area07 chained by doors (DOOR_1..DOOR_6) + WP_ENTER_n waypoints;
  area08 has no door/trigger link at all - only reachable via trg_secret7's
  JumpToLocation(GetLocation(GetWaypointByTag("WP_SECRET"))) in area07's trigger.
- Base-game shadow: nw_c2_default9.nss is a module file with a base-game script's name
  (empty body, otherwise code-identical to filler stubs).
- HAK conflict: at_givekey.ncs and filler_0.uti exist in both the module and marta_hak
  with different content.
- Text encoding: Polish (Zgroda żółta) and Cyrillic (Привет) strings
  in area names / dlg text; Comment field on bad_comment_item.uti has raw 0x81/0x8D bytes.
- XSS probes: item xss_item.uti name and dlg_xss.dlg's entry text both contain
  "<script>alert(1)</script>".
- Quests: 6 in module.jrl (q_keeper, q_relic, q_ghost, q_bandits, q_lost,
  q_never_updated); q_never_updated has no updater anywhere; q_missing_from_journal
  is referenced by big_dlg but not a real quest.
- Plot/quest-linked items: sword_of_kings, amulet_ghost, crown_relic (Plot=1),
  looked up via GetItemPossessedBy() in scripts/conditions and dlg conditions.
- File name length: bad_include_caller (18 chars) and inc_diamond_right (17 chars)
  both exceed NWN's 16-char resource name limit.

## Expected impact (~10 resources)
- inc_a / inc_d: Critical (reached from module OnLoad via mod_load_marta).
- ae_enter1..7: High (module-wide OnEnter pattern across many areas + quest update).
- sc_is_child_cond: High (30 uses across the big dialogue).
- sharedtag_0..3: Medium (placed as a door key but ambiguous tag).
- give_gold_a/b/c, orphan_action_0..4, filler_script_*: None (unreferenced).
- nw_c2_default9: should be Review, never Safe (shadows a base-game resref).

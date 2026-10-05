# Ground truth for dave_module (built by make_dave_module.py)

Truly in-use (reachable from module.ifo): mod_pw_load/mod_client_enter/mod_player_death/
mod_player_rest/mod_player_chat (module events), pw_inc (included by 10 scripts), all
area_*_enter scripts, all trig_enter_d* scripts, door_open_arena, door_open_d3 (+ fires
door_custom via SetEventScript), door_fail_d3 (wrong-type conditional), dlg_act_reward
(+ duplicates dup_reward_a/b, unreferenced), rat_death, wolf_death, cond_has_pstone (dlg
conditional), tb_healkit/tb_portalstone (tag-based, item tags HEALKIT/PORTALSTONE + prefix
tb_), pw_persist (SetCampaignInt/SqlPrepareQueryCampaign), pw_crlf_test/pw_bom_test (CRLF
+ UTF-8 BOM, unreferenced but present), pw_seventeen_char (17-char name, unreferenced),
most items placed in hub/dungeon1/dungeon2/dungeon3/arena, key_dungeon2 (door KeyName match).

Truly unused / safe-delete candidates: unused_item_a/b.uti, door_key_unused.uti,
old_unused.nss + old_lib.nss (include group), compiled_evt.ncs (no source),
unused_convo_sc.nss + merchant_ex_dlg.dlg (unused conversation group), unused_scen_tex.tga,
heartbeat_hub.nss (never wired - test gap, not toolkit's fault),
quest_update_a/b.nss (never wired - test gap), area:abandoned_test + area_aband_entr.nss
(not in Mod_Area_list, nothing transitions to it).

Dynamic / Review: dyn_event referenced only via ExecuteScript("dyn_" + "event", ...) concat
in area_hub_enter.nss (should be flagged Review, never auto-deleted); spawner_engine itself
is unreferenced (test gap - never wired as OnHeartbeat, but its own GetWaypointByTag +
CreateObject("spawn_" + GetLocalString(...)) pattern is the thing under test); po_dave_h.tga
portrait naming; c_ratm.mdl/c_ratm_tex.tga (creature model+texture, kept regardless).

Duplicates (expected): sword_a == sword_b (identical item); npc_carried_item /
d1_loose_item / unused_item_b (same stats, different name - functional dup, review);
area_aband_entr / area_d2_enter / door_open_arena / door_open_d3 / unused_convo_sc /
door_custom / heartbeat_hub / pw_crlf_test / pw_seventeen_char / rat_death / wolf_death /
dyn_event - all normalise to the same empty `void main() { }` body (accidental/realistic
mega-cluster of stub scripts - see report finding #3); trig_enter_d1/d2/d3 identical;
dlg_act_reward / dup_reward_a / dup_reward_b identical.

Impact (~10 expected): pw_inc -> Critical (module-wide via mod_client_enter's include);
mod_pw_load -> Critical (Mod_OnModLoad); pw_persist -> Critical (module event chain);
tb_healkit/tb_portalstone -> Medium (tag-based reachable); dyn_event -> None (dynamic,
not traced as carrier); area:abandoned_test -> Medium even though orphaned/unreachable
(see finding #4 - impact doesn't consider reachability); door_custom -> Medium once wired
via SetEventScript; spawner_engine -> None (never wired - test gap); door_fail_d3 ->
flagged wrong_script_type issue (StartingConditional used as OnFailToOpen, correct);
item_portalstone -> High (dlg + quest + tag-based script + chest placement).

Items (15): sword_a/sword_b (dup pair), item_healkit (tag-based, hub chest),
item_portalstone (tag-based, dungeon2 chest), key_dungeon2 (door key/KeyName),
store_only_item + vendor_item_ex (hub store, 2 items), chest_only_item (dungeon2 chest
only), reward_item_vc (CreateItemOnObject-only, 3 creator scripts), unused_item_a/b
(fully unused), npc_carried_item (carried by hub NPC), arena_prize_item (arena chest),
d1_loose_item (dungeon1 chest), door_key_unused (fully unused key).

Module: Mod_HakList has pw_hak1 (real, passed via --hak) and pw_hak2_missing (deliberately
absent) -> triggers hak_not_loaded error AND downgrades every single Review/Safe verdict
in the whole module to Review (see report finding #1 - not scoped to hak-related files).
30 VarTable entries (PW_CFG_1..30) + MODULE_VAR_TAGBASED_SCRIPT_PREFIX="tb_".
6 areas total, 5 in Mod_Area_list (hub/dungeon1/dungeon2/dungeon3/arena), arena reachable
ONLY via door_arena's LinkedTo (no trigger, no area-list entry pointing to it directly -
it's still in Mod_Area_list itself but its only in-world entrance is the door).
abandoned_test area (6th) is NOT in Mod_Area_list and nothing transitions to it.

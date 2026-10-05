// Minimal stand-in for the game's nwscript.nss so the test suite can compile
// scripts without a Neverwinter Nights install. NOT for real modules.
#define ENGINE_NUM_STRUCTURES   0
int    TRUE  = 1;
int    FALSE = 0;
string sLanguage = "nwscript";
object OBJECT_SELF = 0x7f000000;
object OBJECT_INVALID = 0x7f000001;
string IntToString(int nInteger);
object GetEnteringObject();
object GetModule();
int GetLocalInt(object oObject, string sVarName);
void SetLocalInt(object oObject, string sVarName, int nValue);
void AddJournalQuestEntry(string szPlotID, int nState, object oCreature, int bAllPartyMembers=TRUE, int bAllPlayers=FALSE, int bAllowOverrideHigher=FALSE);
void ExecuteScript(string sScript, object oTarget=OBJECT_SELF);
object GetItemPossessedBy(object oCreature, string sItemTag);
object GetLastOpenedBy();
int GetIsObjectValid(object oObject);
object GetPCSpeaker();
object CreateItemOnObject(string sItemTemplate, object oTarget=OBJECT_SELF, int nStackSize=1, string sNewTag="");

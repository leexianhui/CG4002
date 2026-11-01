// The runner inserts the actual firmware selection function below these
// deterministic candidate providers. This exercises selection control flow
// without Arduino, IMU hardware, or a host-executable ESP32 binary.
enum WeaponType { WEAPON_SWORD_SHIELD };
constexpr WeaponType lockedWeapon = WEAPON_SWORD_SHIELD;
constexpr float SWORD_UPPERCUT_MAX_DIST = 2.25f;
constexpr float P2_UPPERCUT_MAX_DIST = 1.50f;
struct TemplateMatch { int idx; float dist; };
struct Stroke { TemplateMatch nearest, relaxed, playerTwo; };
constexpr TemplateMatch nearestTemplate(const Stroke &s) { return s.nearest; }
constexpr TemplateMatch relaxedSwordUppercut(const Stroke &s) { return s.relaxed; }
constexpr TemplateMatch playerTwoUppercut(const Stroke &s) { return s.playerTwo; }

// INSERT STRIKE SELECTION HERE

constexpr bool select(const Stroke &s, int expectedIdx, bool expectedRelaxed)
{
    bool relaxed = true;
    TemplateMatch m = chooseStrikeTemplate(s, relaxed);
    return m.idx == expectedIdx && relaxed == expectedRelaxed;
}

// Logged t0=294930: spin distance .81 hid an uppercut-grip distance .542.
// A base match already inside its limit must not suppress alternate matching.
static_assert(select({{4, .81f}, {-1, 1e9f}, {3, .542f}}, 3, true),
              "Consider the closer P2 uppercut even when spin is already in range");
static_assert(select({{4, .70f}, {-1, 1e9f}, {3, 1.60f}}, 4, false),
              "An out-of-limit uppercut must not override a valid spin");
static_assert(select({{4, .40f}, {3, 1.10f}, {3, .542f}}, 4, false),
              "Keep the closer eligible spin");
static_assert(select({{0, 3.0f}, {3, 2.1f}, {-1, 1e9f}}, 3, true),
              "The relaxed sword uppercut fallback must still work");
static_assert(select({{0, 3.0f}, {3, 2.1f}, {3, .8f}}, 3, true),
              "Compare both uppercut grips");
static_assert(select({{0, 3.0f}, {-1, 1e9f}, {-1, 1e9f}}, 0, false),
              "A missing alternate must not change the base match or relaxed flag");

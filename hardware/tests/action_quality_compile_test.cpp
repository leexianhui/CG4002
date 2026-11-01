// Compile-time regression checks: no board or host test framework required.
// Run action_quality_compile_test.ps1 with a C++17 compiler.
// The runner prepends the actual inline checks extracted from each sketch.
using Rejection = ActionQuality::Rejection;
using ActivityRun = ActionQuality::ActivityRun;

static_assert(!ActionQuality::templateEligible(true, 60),
              "A spin with 60ms of torso activity must not compete against an uppercut");
static_assert(ActionQuality::templateEligible(false, 0),
              "A cut or uppercut must not inherit the spin-only torso rule");
static_assert(ActionQuality::templateEligible(true, 100),
              "A qualifying spin must still compete");

static_assert(ActionQuality::evaluate(60, 50, false, 0) == Rejection::TOO_SHORT,
              "A strong brief flick must fail");
static_assert(ActionQuality::evaluate(500, 20, false, 0) == Rejection::WEAK_HAND,
              "A long weak adjustment must fail");
static_assert(ActionQuality::evaluate(100, 40, false, 0) == Rejection::NONE,
              "An ordinary strike at the thresholds needs no torso movement");
static_assert(ActionQuality::evaluate(600, 100, true, 0) == Rejection::QUIET_SPIN_TORSO,
              "A wrist-only movement must not pass as a spin");
static_assert(ActionQuality::evaluate(600, 100, true, 100) == Rejection::NONE,
              "A spin with sustained hand and torso activity must pass");
static_assert(ActionQuality::evaluate(99, 40, false, 0) == Rejection::TOO_SHORT, "Duration boundary");
static_assert(ActionQuality::evaluate(100, 39, false, 0) == Rejection::WEAK_HAND, "Hand boundary");
static_assert(ActionQuality::evaluate(100, 40, true, 99) == Rejection::QUIET_SPIN_TORSO, "Torso boundary");

constexpr uint32_t wristRun(bool spike, bool gap, bool interruption)
{
    ActivityRun run;
    for (uint32_t t = 0; t <= 100; t += 10)
        if (!gap || t == 0 || t >= 80)
            run.update(t, spike ? t == 50 : !(interruption && t == 50), ActionQuality::MAX_WRIST_GAP_MS);
    return run.longestMs;
}
static_assert(wristRun(true, false, false) == 0, "One spike is not sustained motion");
static_assert(wristRun(false, true, false) == 20, "A sample gap cannot inflate motion time");
static_assert(wristRun(false, false, true) == 40, "Below-threshold data resets the run");
static_assert(wristRun(false, false, false) == 100, "Consecutive active samples accumulate");

constexpr uint32_t torsoRun(bool missing)
{
    ActivityRun run;
    for (uint32_t t = 0; t <= 120; t += 20)
        if (!missing || t == 0 || t >= 80)
            run.update(t, true, ActionQuality::MAX_TORSO_GAP_MS);
    return run.longestMs;
}
static_assert(torsoRun(false) == 120, "Sustained torso activity accumulates");
static_assert(torsoRun(true) == 40, "Missing beacons cannot satisfy the spin gate");

constexpr uint32_t rolloverRun()
{
    ActivityRun run;
    run.update(UINT32_MAX - 19, true, ActionQuality::MAX_WRIST_GAP_MS);
    run.update(UINT32_MAX - 9, true, ActionQuality::MAX_WRIST_GAP_MS);
    run.update(0, true, ActionQuality::MAX_WRIST_GAP_MS);
    run.update(10, true, ActionQuality::MAX_WRIST_GAP_MS);
    return run.longestMs;
}
static_assert(rolloverRun() == 30, "Activity tracking survives millis rollover");

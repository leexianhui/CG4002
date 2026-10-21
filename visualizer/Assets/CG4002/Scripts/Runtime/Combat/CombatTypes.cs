namespace CG4002.Combat
{
    // Values are explicit because Unity serializes enums as ints: reordering
    // members would silently remap every asset that stores one.

    public enum WeaponType
    {
        SwordAndShield = 0,
        Spear = 1,
        Mace = 2,
    }

    /// <summary>
    /// Attack classes recognised by the per-weapon classifiers (design report 5.2.2).
    /// Which of these a weapon supports is data, held by its WeaponDefinition.
    /// </summary>
    public enum ActionType
    {
        Idle = 0,
        HorizontalSwing = 1,
        VerticalSwing = 2,
        DiagonalSwing = 3,
        Thrust = 4,
        ChargedAttack = 5,
        SpinAttack = 6,
        ChargedThrust = 7,
        Swing = 8,
        OverheadStrike = 9,
        BackStrike = 10,
        Uppercut = 11,
    }

    /// <summary>
    /// Animator attack states: one per motion, each weapon supplying its own clip for every
    /// state. Several actions may share a slot (e.g. the spear's SWING plays Horizontal). The
    /// state is named after the member, so adding one means regenerating the models.
    /// </summary>
    public enum AttackSlot
    {
        Horizontal = 0,
        Charged = 1,
        Vertical = 2,
        Diagonal = 3,
        Thrust = 4,
        Spin = 5,
        Uppercut = 6,
    }
}

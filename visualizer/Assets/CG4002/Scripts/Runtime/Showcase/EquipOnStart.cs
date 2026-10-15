using CG4002.Models;
using UnityEngine;

namespace CG4002.Showcase
{
    /// <summary>
    /// Equips a weapon when the scene starts, for test scenes with no Game Engine connection.
    /// The real visualizer equips from WEAPON_CHANGED messages instead.
    /// </summary>
    [RequireComponent(typeof(AvatarView))]
    public class EquipOnStart : MonoBehaviour
    {
        [SerializeField] WeaponDefinition weapon;

        void Start()
        {
            if (weapon != null)
                GetComponent<AvatarView>().Equip(weapon);
        }
    }
}

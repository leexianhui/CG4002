using CG4002.Combat;
using UnityEngine;

namespace CG4002.Models
{
    /// <summary>Lookup from the weapon ID in a WEAPON_CHANGED message to its definition.</summary>
    [CreateAssetMenu(menuName = "CG4002/Weapon Catalog")]
    public class WeaponCatalog : ScriptableObject
    {
        public WeaponDefinition[] weapons;

        public WeaponDefinition Get(WeaponType type)
        {
            foreach (var weapon in weapons)
            {
                if (weapon != null && weapon.type == type)
                    return weapon;
            }
            return null;
        }
    }
}

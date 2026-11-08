using System;
using System.Runtime.InteropServices;
using CG4002.Combat;
using CG4002.Models;
using CG4002.State;
using CG4002.UI;
using UnityEngine;

namespace CG4002.FX
{
    /// <summary>
    /// Sound effects and music. Like the HUD it only listens: every sound is triggered by an event
    /// the game already raises, so it never changes what happens.
    ///
    ///   your attack recognised     swing, per weapon (sword light, spear long, mace heavy and pitched down)
    ///   enemy attacks              its swing, played from where the enemy stands
    ///   outcome                    hit thud / block (shield thud or metal clash) / parry ring / miss whiff,
    ///                              from the defender's side
    ///   guard raised               armour rattle
    ///   weapon locked              sword unsheathed
    ///   match ended                victory fanfare / defeat jingle (the music dips under it)
    ///
    /// Music: a calm track before the match (connecting, weapon selection), a battle track during it,
    /// cross-faded, and kept soft under the effects. Volumes are fixed; players use the phone's volume.
    /// On iPhone the game plays even with the ring/silent switch on (see Plugins/iOS/CG4002AudioSession.mm).
    /// </summary>
    public class GameAudio : MonoBehaviour
    {
        [Serializable]
        public class WeaponSwings
        {
            public WeaponType weapon;
            public AudioClip[] clips;
            [Range(0.5f, 1.5f)] public float pitch = 1f;
        }

        [SerializeField] MessageDispatcher dispatcher;
        [SerializeField] GameState gameState;
        [Tooltip("The enemy's sounds play from here, in 3D.")]
        [SerializeField] AvatarView opponent;
        [Tooltip("Optional. Calm music while it's open; plays the equip sound on a weapon lock.")]
        [SerializeField] WeaponSelectScreen weaponSelect;

        [Header("Effects")]
        [SerializeField] WeaponSwings[] swings = Array.Empty<WeaponSwings>();
        [SerializeField] AudioClip[] hits = Array.Empty<AudioClip>();
        [Tooltip("Blocked by a shield (sword & shield).")]
        [SerializeField] AudioClip[] shieldBlocks = Array.Empty<AudioClip>();
        [Tooltip("Blocked by a weapon (spear, mace).")]
        [SerializeField] AudioClip[] weaponBlocks = Array.Empty<AudioClip>();
        [SerializeField] AudioClip parry, miss, guard, equip, victory, defeat;

        [Header("Music")]
        [SerializeField] AudioClip calmMusic;
        [SerializeField] AudioClip battleMusic;

        [Header("Mix (fixed)")]
        [SerializeField, Range(0f, 1f)] float effectsVolume = 1f;
        [SerializeField, Range(0f, 1f)] float musicVolume = 0.25f;
        [SerializeField, Range(0f, 1f)] float stingerVolume = 0.8f;
        [SerializeField] float crossfadeSeconds = 1.5f;

        AudioSource near;     // your own sounds and stingers: plain stereo
        AudioSource enemy;    // the enemy's sounds: 3D, follows the enemy
        AudioSource musicA, musicB, playing;
        AudioClip wantedMusic;
        float duckUntil;

#if UNITY_IOS && !UNITY_EDITOR
        [DllImport("__Internal")] static extern void CG4002_PlayEvenWhenSilent();
#endif

        void Awake()
        {
            near = NewSource("Near", spatial: false);
            enemy = NewSource("Enemy", spatial: true);
            musicA = NewSource("Music A", spatial: false);
            musicB = NewSource("Music B", spatial: false);
            foreach (var music in new[] { musicA, musicB })
            {
                music.loop = true;
                music.volume = 0f;
            }
            PlayEvenWhenSilent();
        }

        void OnEnable()
        {
            dispatcher.ActionRecognized += OnActionRecognized;
            dispatcher.ActionReceived += OnActionReceived;
            dispatcher.OutcomeReceived += OnOutcomeReceived;
            gameState.GuardChanged += OnGuardChanged;
            gameState.PhaseChanged += OnPhaseChanged;
            if (weaponSelect != null)
                weaponSelect.WeaponLocked += OnWeaponLocked;
        }

        void OnDisable()
        {
            dispatcher.ActionRecognized -= OnActionRecognized;
            dispatcher.ActionReceived -= OnActionReceived;
            dispatcher.OutcomeReceived -= OnOutcomeReceived;
            gameState.GuardChanged -= OnGuardChanged;
            gameState.PhaseChanged -= OnPhaseChanged;
            if (weaponSelect != null)
                weaponSelect.WeaponLocked -= OnWeaponLocked;
        }

        // iOS can reset the audio session when the app comes back, so set it again.
        void OnApplicationPause(bool paused)
        {
            if (!paused)
                PlayEvenWhenSilent();
        }

        static void PlayEvenWhenSilent()
        {
#if UNITY_IOS && !UNITY_EDITOR
            CG4002_PlayEvenWhenSilent();
#endif
        }

        // ---- Effects ----------------------------------------------------------------------

        void OnActionRecognized(ActionRecognizedMessage message)
        {
            // Your own attack: the same moment the first-person animation starts.
            if (message.player == gameState.LocalPlayer && !message.onCooldown)
                Swing(near, gameState.Self.weapon);
        }

        void OnActionReceived(ActionMessage message)
        {
            if (message.player == gameState.OpponentPlayer)
                Swing(enemy, gameState.Opponent.weapon);
        }

        void OnOutcomeReceived(CombatOutcomeMessage message)
        {
            // message.player is the defender: the impact sounds from their side.
            var source = message.player == gameState.LocalPlayer ? near : enemy;
            switch (message.outcome)
            {
                case Outcome.Hit:
                    Play(source, Pick(hits), 1f);
                    break;
                case Outcome.Block:
                    bool shield = gameState.Get(message.player).weapon == WeaponType.SwordAndShield;
                    Play(source, Pick(shield ? shieldBlocks : weaponBlocks), 1f);
                    break;
                case Outcome.Parry:
                    Play(source, parry, 1f);
                    break;
                case Outcome.Miss:
                    Play(message.attacker == gameState.LocalPlayer ? near : enemy, miss, 0.6f, pitch: 1.2f);
                    break;
            }
        }

        void OnGuardChanged(PlayerId player, bool guarding)
        {
            if (guarding)
                Play(player == gameState.LocalPlayer ? near : enemy, guard, 0.7f);
        }

        void OnWeaponLocked(WeaponType weapon) => Play(near, equip, 1f);

        void OnPhaseChanged(MatchPhase phase)
        {
            if (phase != MatchPhase.Ended)
                return;
            var stinger = gameState.Winner == gameState.LocalPlayer ? victory : defeat;
            if (stinger == null)
                return;
            near.PlayOneShot(stinger, stingerVolume);
            duckUntil = Time.time + stinger.length;
        }

        void Swing(AudioSource source, WeaponType weapon)
        {
            foreach (var set in swings)
            {
                if (set.weapon == weapon)
                {
                    Play(source, Pick(set.clips), 1f, set.pitch);
                    return;
                }
            }
        }

        void Play(AudioSource source, AudioClip clip, float volume, float pitch = 1f)
        {
            if (clip == null)
                return;
            // A little random pitch so repeated hits don't sound identical.
            source.pitch = pitch * UnityEngine.Random.Range(0.95f, 1.05f);
            source.PlayOneShot(clip, volume * effectsVolume);
        }

        static AudioClip Pick(AudioClip[] clips) =>
            clips == null || clips.Length == 0 ? null : clips[UnityEngine.Random.Range(0, clips.Length)];

        // ---- Music and placement -----------------------------------------------------------

        void LateUpdate()
        {
            if (opponent != null)
                enemy.transform.position = opponent.transform.position + Vector3.up;

            // Calm before the match (connecting, choosing a weapon), the battle track during it.
            bool selecting = weaponSelect != null && weaponSelect.IsSelecting;
            var wanted = selecting || gameState.Phase == MatchPhase.Waiting ? calmMusic : battleMusic;
            if (wanted != wantedMusic)
                SwitchMusic(wanted);

            float duck = Time.time < duckUntil ? 0.35f : 1f;
            float step = Time.deltaTime / Mathf.Max(crossfadeSeconds, 0.01f) * musicVolume;
            foreach (var music in new[] { musicA, musicB })
            {
                float target = music == playing && music.clip != null ? musicVolume * duck : 0f;
                music.volume = Mathf.MoveTowards(music.volume, target, step * 2f);
                if (music != playing && music.isPlaying && music.volume <= 0f)
                    music.Stop();
            }
        }

        void SwitchMusic(AudioClip clip)
        {
            wantedMusic = clip;
            playing = playing == musicA ? musicB : musicA;
            playing.clip = clip;
            if (clip != null)
                playing.Play();
        }

        AudioSource NewSource(string name, bool spatial)
        {
            var source = new GameObject(name).AddComponent<AudioSource>();
            source.transform.SetParent(transform, false);
            source.playOnAwake = false;
            source.spatialBlend = spatial ? 1f : 0f;
            if (spatial)
            {
                source.rolloffMode = AudioRolloffMode.Linear;
                source.minDistance = 1f;
                source.maxDistance = 25f;
            }
            return source;
        }
    }
}

using System.Collections;
using CG4002.Combat;
using CG4002.Controllers;
using CG4002.Models;
using CG4002.Net;
using CG4002.State;
using UnityEngine;
using UnityEngine.UI;

namespace CG4002.UI
{
    /// <summary>
    /// The tutorial's lessons, while the engine says this phone's player is in the tutorial (TUTORIAL
    /// in the online panel; the engine plays the other player as a dummy standing where the arena was
    /// placed). Setup is the usual one (place, calibrate, choose a weapon); once the match runs:
    ///
    ///   7 lessons, one at a time: the six attacks, then the guard. The dummy, mirrored and holding
    ///   your weapon, demonstrates the move every few seconds; the title names it and the cue line
    ///   says how. You do it for real (wearables, AI, engine). The move the AI recognised shows as
    ///   usual (FirstPersonAttack's label); the asked-for one shows CORRECT MOVE and the next lesson
    ///   starts. The guard lesson passes when the engine reports your guard up.
    ///   Then TUTORIAL COMPLETE and free practice: the dummy raises its guard now and then, until
    ///   EXIT TUTORIAL in the panel.
    ///
    /// Progress counts recognition, not hitting the dummy. Inert in a normal match and offline.
    /// </summary>
    public class TutorialController : MonoBehaviour
    {
        [SerializeField] GameState gameState;
        [SerializeField] MessageDispatcher dispatcher;
        [Tooltip("Optional (OnlineScene): told when the lessons are done, to start free practice.")]
        [SerializeField] OnlineEngineClient client;
        [Tooltip("The opponent avatar: the engine's dummy during the tutorial, which demonstrates each move.")]
        [SerializeField] AvatarController dummy;
        [SerializeField] Text title;
        [SerializeField] Text cue;

        [Header("Timing")]
        [SerializeField] float demoEverySeconds = 4f;
        [SerializeField] float guardDemoSeconds = 2f;
        [SerializeField] float correctSeconds = 1.5f;
        [SerializeField] float completeSeconds = 5f;

        static readonly Color CueColour = new(0.93f, 0.89f, 0.8f);
        static readonly Color CorrectColour = new(0.45f, 1f, 0.45f);

        // Null action: the guard lesson.
        static readonly (ActionType? action, string name, string cue)[] Lessons =
        {
            (ActionType.HorizontalSwing, "HORIZONTAL SWING", "Cock it to the right, slash across to the left"),
            (ActionType.VerticalSwing, "VERTICAL SWING", "Raise it overhead, chop straight down"),
            (ActionType.DiagonalSwing, "DIAGONAL SWING", "From high right, cut down to low left"),
            (ActionType.Thrust, "THRUST", "Draw your elbow back, lunge straight forward"),
            (ActionType.SpinAttack, "SPIN ATTACK", "Wind up right, turn a full circle with the weapon out"),
            (ActionType.Uppercut, "UPPERCUT", "Dip low to your knee, drive it up overhead"),
            (null, "GUARD", "Raise your guard and hold it"),
        };

        enum Stage { Off, Setup, Lesson, Correct, Complete, Practice }

        Stage stage;
        int lesson;
        float stageTime;
        float nextDemo;
        Coroutine guardDemo;
        Vector3? dummyScale;

        bool InMyTutorial => gameState.TutorialPlayer == gameState.LocalPlayer;
        AvatarView DummyView => dummy != null ? dummy.View : null;

        void OnEnable()
        {
            dispatcher.ActionRecognized += OnActionRecognized;
            gameState.GuardChanged += OnGuardChanged;
        }

        void OnDisable()
        {
            dispatcher.ActionRecognized -= OnActionRecognized;
            gameState.GuardChanged -= OnGuardChanged;
            Stop();
        }

        void Update()
        {
            if (!InMyTutorial)
            {
                if (stage != Stage.Off)
                    Stop();
                return;
            }
            if (stage == Stage.Off)
            {
                stage = Stage.Setup;
                Show("", "", CueColour);
            }
            // The match runs once this player is placed, calibrated and armed: then the lessons start.
            if (stage == Stage.Setup)
            {
                if (gameState.Phase == MatchPhase.Running)
                    Begin(0);
                return;
            }

            stageTime += Time.deltaTime;
            switch (stage)
            {
                case Stage.Lesson:
                    if (Time.time >= nextDemo)
                        Demonstrate();
                    break;
                case Stage.Correct:
                    if (stageTime >= correctSeconds)
                    {
                        if (lesson + 1 < Lessons.Length)
                            Begin(lesson + 1);
                        else
                            Complete();
                    }
                    break;
                case Stage.Complete:
                    if (stageTime >= completeSeconds)
                    {
                        stage = Stage.Practice;
                        Show("FREE PRACTICE", "", CueColour);
                    }
                    break;
            }
        }

        void Begin(int index)
        {
            lesson = index;
            stage = Stage.Lesson;
            stageTime = 0f;
            nextDemo = Time.time + 0.5f;
            Mirror(true);
            var (_, name, how) = Lessons[index];
            Show(name, $"Lesson {index + 1} of {Lessons.Length}: {how}", CueColour);
        }

        /// <summary>The dummy shows the lesson's move, again every few seconds until it's done.</summary>
        void Demonstrate()
        {
            nextDemo = Time.time + demoEverySeconds;
            var view = DummyView;
            if (view == null || view.CurrentDefinition == null)
                return;
            if (Lessons[lesson].action is ActionType action)
            {
                if (view.CurrentDefinition.TryGetSlot(action, out AttackSlot slot))
                    view.PlayAttack(slot);
            }
            else
            {
                if (guardDemo != null)
                    StopCoroutine(guardDemo);
                guardDemo = StartCoroutine(GuardDemo(view));
            }
        }

        IEnumerator GuardDemo(AvatarView view)
        {
            view.SetGuard(true);
            yield return new WaitForSeconds(guardDemoSeconds);
            view.SetGuard(false);
            guardDemo = null;
        }

        void OnActionRecognized(ActionRecognizedMessage message)
        {
            if (stage == Stage.Lesson && message.player == gameState.LocalPlayer && Lessons[lesson].action == message.action)
                Correct();
        }

        void OnGuardChanged(PlayerId player, bool guarding)
        {
            if (stage == Stage.Lesson && player == gameState.LocalPlayer && guarding && Lessons[lesson].action == null)
                Correct();
        }

        void Correct()
        {
            stage = Stage.Correct;
            stageTime = 0f;
            Show(Lessons[lesson].name, "CORRECT MOVE", CorrectColour);
        }

        void Complete()
        {
            stage = Stage.Complete;
            stageTime = 0f;
            Mirror(false);
            Show("TUTORIAL COMPLETE", "Free practice: the dummy now guards now and then. EXIT TUTORIAL in the panel when done.", CueColour);
            if (client != null)
                client.StartFreePractice();
        }

        void Stop()
        {
            stage = Stage.Off;
            if (guardDemo != null)
            {
                StopCoroutine(guardDemo);
                guardDemo = null;
                if (DummyView != null)
                    DummyView.SetGuard(false);
            }
            Mirror(false);
            Show("", "", CueColour);
        }

        /// <summary>Mirrored, the dummy's weapon hand is on the same side as yours, so you copy it like a mirror.</summary>
        void Mirror(bool mirrored)
        {
            var view = DummyView;
            if (view == null)
                return;
            var t = view.transform;
            dummyScale ??= t.localScale;
            Vector3 scale = dummyScale.Value;
            t.localScale = new Vector3(mirrored ? -Mathf.Abs(scale.x) : Mathf.Abs(scale.x), scale.y, scale.z);
        }

        void Show(string heading, string line, Color lineColour)
        {
            if (title != null)
            {
                title.text = heading;
                title.enabled = heading.Length > 0;
            }
            if (cue != null)
            {
                cue.text = line;
                cue.color = lineColour;
                cue.enabled = line.Length > 0;
            }
        }
    }
}

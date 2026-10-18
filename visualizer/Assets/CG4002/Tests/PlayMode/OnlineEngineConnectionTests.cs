#if UNITY_EDITOR
using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using CG4002.AR;
using CG4002.Controllers;
using CG4002.Net;
using CG4002.State;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;
using Object = UnityEngine.Object;

namespace CG4002.Tests
{
    // Opt-in real network smoke test. AR pose is simulated; the client/dispatcher/state are real.
    public class OnlineEngineConnectionTests
    {
        readonly List<GameObject> roots = new();
        readonly List<OnlineEngineClient> clients = new();
        float nextPosition;

        [UnityTest]
        public IEnumerator TwoUnityClientsReceiveCombatAndRecoverAfterReconnect()
        {
            string url = Environment.GetEnvironmentVariable("CG_UNITY_TEST_URL");
            string token = Environment.GetEnvironmentVariable("CG_UNITY_TEST_TOKEN");
            if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(token))
                Assert.Ignore("Run through comms/scripts/check_unity_online.py for a dedicated mock engine.");
            var first = Create(PlayerId.P1, url, token);
            var second = Create(PlayerId.P2, url, token);
            int attacks = 0, outcomes = 0;
            first.dispatcher.ActionReceived += _ => attacks++;
            first.dispatcher.OutcomeReceived += _ => outcomes++;
            first.Connect(); second.Connect();
            yield return Until(() => first.gameState.Phase == MatchPhase.Running &&
                second.gameState.Phase == MatchPhase.Running, "both Unity clients ready");
            yield return Until(() => first.gameState.Self.health < 100 &&
                first.gameState.Opponent.health < 100, "mock combat reaches Unity state");
            Assert.That(first.gameState.Self.weaponSelected, Is.True);
            Assert.That(attacks, Is.GreaterThan(0));
            Assert.That(outcomes, Is.EqualTo(attacks), "one animation event per resolved combat event");

            first.calibration.Invalidate();
            yield return Until(() => second.gameState.Phase == MatchPhase.Paused,
                "calibration loss pauses even while socket stays connected");
            Assert.That(first.Connected, Is.True);
            Assert.That(first.calibration.ConfirmFromMarkedForwardLine(), Is.True);
            yield return Until(() => second.gameState.Phase == MatchPhase.Running,
                "confirmed calibration and fresh position resume combat");

            first.Disconnect();
            yield return Until(() => second.gameState.Phase == MatchPhase.Paused, "disconnect pauses match");
            int hp = second.gameState.Opponent.health;
            first.calibration.Invalidate(); // Keep recovery paused until resnapshot is checked.
            first.gameState.Apply(new HealthMessage { player = PlayerId.P1, health = 100, maxHealth = 100 });
            first.Connect();
            yield return Until(() => first.gameState.Self.health == hp && first.Connected,
                "reconnect restores authoritative HP");
            Assert.That(first.gameState.Phase, Is.EqualTo(MatchPhase.Paused));
            Assert.That(first.calibration.ConfirmFromMarkedForwardLine(), Is.True);
            yield return Until(() => first.gameState.Phase == MatchPhase.Running &&
                second.gameState.Phase == MatchPhase.Running, "fresh readiness resumes match");
        }

        OnlineEngineClient Create(PlayerId player, string url, string token)
        {
            var root = new GameObject("OnlineSmoke" + player);
            root.SetActive(false);
            roots.Add(root);
            var state = root.AddComponent<GameState>();
            var dispatcher = root.AddComponent<MessageDispatcher>();
            Set(dispatcher, "gameState", state);
            var fixtures = new GameObject("Inactive pose fixtures");
            fixtures.transform.SetParent(root.transform);
            fixtures.SetActive(false); // No AR provider or avatar rendering in this transport test.
            var placer = fixtures.AddComponent<DummyPlacer>();
            Set(placer, "<IsPlaced>k__BackingField", true);
            var camera = fixtures.AddComponent<Camera>();
            camera.transform.position = new Vector3(0, 1.5f, 1);
            var arena = new GameObject("Arena").transform;
            arena.SetParent(root.transform);
            var calibration = root.AddComponent<SharedArenaCalibration>();
            calibration.placer = placer;
            calibration.arena = arena;
            calibration.arCamera = camera;
            calibration.requireARTracking = false;
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.True);
            var client = root.AddComponent<OnlineEngineClient>();
            client.loadSavedSettings = false;
            client.gameState = state;
            client.dispatcher = dispatcher;
            client.opponent = fixtures.AddComponent<AvatarController>();
            client.calibration = calibration;
            client.player = player;
            client.endpoint = url;
            client.bearerToken = token;
            clients.Add(client);
            root.SetActive(true);
            return client;
        }

        IEnumerator Until(Func<bool> condition, string description)
        {
            float deadline = Time.realtimeSinceStartup + 20f;
            while (!condition())
            {
                Assert.That(Time.realtimeSinceStartup, Is.LessThan(deadline), description);
                if (Time.realtimeSinceStartup >= nextPosition)
                {
                    nextPosition = Time.realtimeSinceStartup + 0.1f;
                    foreach (var client in clients)
                        client.dispatcher.Outbound.Enqueue(new PositionMessage {
                            player = client.player,
                            position = new Vector3((int)client.player - 1, 0, 0),
                            headingDegrees = client.player == PlayerId.P1 ? 90 : 270,
                        });
                }
                yield return null;
            }
        }

        [UnityTearDown]
        public IEnumerator CleanUp()
        {
            foreach (var client in clients) client.Disconnect();
            foreach (var root in roots) Object.Destroy(root);
            clients.Clear(); roots.Clear();
            yield return null;
        }

        static void Set(object target, string name, object value) => target.GetType()
            .GetField(name, BindingFlags.Instance | BindingFlags.NonPublic).SetValue(target, value);
    }
}
#endif

#if UNITY_EDITOR
using System.Reflection;
using CG4002.AR;
using CG4002.Net;
using NUnit.Framework;
using UnityEngine;

namespace CG4002.Tests
{
    public class PhoneCalibrationTests
    {
        GameObject root;
        SharedArenaCalibration calibration;

        [SetUp]
        public void SetUp()
        {
            root = new GameObject("Calibration test");
            root.SetActive(false);
            calibration = root.AddComponent<SharedArenaCalibration>();
            calibration.requireARTracking = false;
            calibration.placer = root.AddComponent<DummyPlacer>();
            typeof(DummyPlacer).GetField("<IsPlaced>k__BackingField",
                BindingFlags.Instance | BindingFlags.NonPublic).SetValue(calibration.placer, true);
            var cameraObject = new GameObject("Camera");
            cameraObject.transform.SetParent(root.transform);
            calibration.arCamera = cameraObject.AddComponent<Camera>();
            calibration.arena = new GameObject("Arena").transform;
            calibration.arena.SetParent(root.transform);
            calibration.arCamera.transform.position = new Vector3(0, 1.5f, 1);
        }

        [TearDown] public void CleanUp() => Object.DestroyImmediate(root);

        [Test]
        public void TooCloseToOriginCannotConfirmDirection()
        {
            calibration.arCamera.transform.position = new Vector3(0, 1.5f, 0.1f);
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.False);
            Assert.That(calibration.IsReady, Is.False);
        }

        [Test]
        public void OriginMovementAndBackgroundInvalidationRequireConfirmation()
        {
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.True);
            calibration.arena.position += Vector3.right * 0.02f;
            Assert.That(calibration.IsReady, Is.False);
            calibration.arena.position -= Vector3.right * 0.02f;
            Assert.That(calibration.IsReady, Is.False, "moving back cannot silently resume");
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.True);
            calibration.Invalidate();
            Assert.That(calibration.IsReady, Is.False);
        }

        [Test]
        public void TrackingLossCannotSilentlyResumeOrConfirm()
        {
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.True);
            calibration.requireARTracking = true; // No AR provider exists in this test.
            Assert.That(calibration.IsReady, Is.False);
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.False);
            calibration.requireARTracking = false;
            Assert.That(calibration.IsReady, Is.False);
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.True);
        }

        [Test]
        public void DifferentWorldOriginsProduceTheSameArenaCoordinates()
        {
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.True);
            var first = PositionReporter.ToArena(calibration.arena, new Vector3(2, 1.5f, 3), Vector3.right);
            var rotation = Quaternion.Euler(0, 73, 0);
            var offset = new Vector3(10, 0, -7);
            calibration.arena.position = offset;
            calibration.arCamera.transform.position = offset + rotation * new Vector3(0, 1.5f, 1);
            Assert.That(calibration.ConfirmFromMarkedForwardLine(), Is.True);
            var second = PositionReporter.ToArena(calibration.arena,
                offset + rotation * new Vector3(2, 1.5f, 3), rotation * Vector3.right);
            Assert.That(Vector3.Distance(first.position, second.position), Is.LessThan(0.001f));
            Assert.That(Mathf.DeltaAngle(first.headingDegrees, second.headingDegrees), Is.EqualTo(0).Within(0.001f));
        }

        [Test]
        public void SetupReadingsRespectPlayerIdentityAndMissingPositions()
        {
            const string json = @"{""phase"":""PAUSED"",""reason"":""NOT_READY"",""players"": [
                {""playerId"":2,""connected"":true,""ready"":true,""position"":{""x"":3,""y"":4,""heading"":180}},
                {""playerId"":1,""connected"":true,""ready"":false,""position"":{""x"":0,""y"":0,""heading"":0}}]}";
            var status = PhoneSetupStatus.Read(json);
            Assert.That(status.Separation, Is.EqualTo(5f));
            Assert.That(status.players[0].ready, Is.False);
            status = PhoneSetupStatus.Read(json.Replace(@"{""x"":0,""y"":0,""heading"":0}", "null"));
            Assert.That(status.Separation, Is.Null);
            Assert.That(status.PlayerLine(0), Does.Contain("position unavailable"));
        }
    }
}
#endif

using CG4002.Offline;
using NUnit.Framework;
using UnityEngine;

namespace CG4002.Tests
{
    /// <summary>The contact test between the blade and the dummy's capsule.</summary>
    public class SwordHitDetectorTests
    {
        // A dummy standing at the origin: capsule from 0.3 m to 1.7 m high, 0.25 m radius.
        static readonly Vector3 Bottom = new(0f, 0.3f, 0f);
        static readonly Vector3 Top = new(0f, 1.7f, 0f);
        const float Radius = 0.25f;

        static bool Touches(Vector3 a, Vector3 b) =>
            SwordHitDetector.SegmentTouchesCapsule(a, b, Bottom, Top, Radius);

        [Test]
        public void BladeThroughTheBody_Touches()
        {
            Assert.That(Touches(new Vector3(-0.5f, 1.2f, 0f), new Vector3(0.5f, 1.2f, 0f)), Is.True);
        }

        [Test]
        public void BladeBesideTheBody_DoesNotTouch()
        {
            Assert.That(Touches(new Vector3(0.6f, 1.0f, -0.3f), new Vector3(0.6f, 1.0f, 0.3f)), Is.False);
        }

        [Test]
        public void TipJustInsideTheRadius_Touches()
        {
            Assert.That(Touches(new Vector3(0.9f, 1.0f, 0f), new Vector3(0.2f, 1.0f, 0f)), Is.True);
        }

        [Test]
        public void BladeAboveTheHead_DoesNotTouch()
        {
            // Top of the capsule is 1.7 m + 0.25 m radius.
            Assert.That(Touches(new Vector3(-0.5f, 2.1f, 0f), new Vector3(0.5f, 2.1f, 0f)), Is.False);
        }

        [Test]
        public void FastSwingJumpingPastTheBody_IsCaughtBySweptPath()
        {
            // Last frame the tip was on the dummy's left, this frame on its right: neither end
            // is inside, but the path between them crosses the body.
            Vector3 previousTip = new(-0.6f, 1.2f, 0f);
            Vector3 tip = new(0.6f, 1.2f, 0f);
            Assert.That(Touches(previousTip, previousTip), Is.False);
            Assert.That(Touches(tip, tip), Is.False);
            Assert.That(Touches(previousTip, tip), Is.True);
        }

        [Test]
        public void VeryFastSwing_StillCaught()
        {
            // 3 m between frames: wider than the body many times over.
            Assert.That(Touches(new Vector3(-1.5f, 1.2f, 0.1f), new Vector3(1.5f, 1.2f, 0.1f)), Is.True);
        }
    }
}

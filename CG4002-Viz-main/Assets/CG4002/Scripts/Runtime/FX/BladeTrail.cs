using System.Collections.Generic;
using UnityEngine;

namespace CG4002.FX
{
    /// <summary>
    /// A ribbon swept between two moving points, e.g. the base and tip of a blade.
    /// Unity's TrailRenderer follows a single point, which gives a line rather than the
    /// band a swinging blade covers, so this builds the mesh itself.
    /// Vertices are in world space: keep this object unparented at the origin.
    /// </summary>
    [RequireComponent(typeof(MeshFilter), typeof(MeshRenderer))]
    public class BladeTrail : MonoBehaviour
    {
        static readonly int TintId = Shader.PropertyToID("_TintColor");

        struct Sample
        {
            public Vector3 basePos;
            public Vector3 tipPos;
            public float time;
        }

        [Tooltip("Seconds a sample stays visible.")]
        public float lifetime = 0.15f;

        [Tooltip("Extra points inserted between frames so fast swings still curve smoothly.")]
        [Range(1, 8)] public int subdivisions = 4;

        [Tooltip("How far the inner edge shrinks toward the tip as the trail ages (0 to 1).")]
        [Range(0f, 1f)] public float taper = 0.6f;

        readonly List<Sample> samples = new();
        readonly List<Vector3> vertices = new();
        readonly List<Color> colors = new();
        readonly List<Vector2> uvs = new();
        readonly List<int> triangles = new();

        Transform baseAnchor;
        Transform tipAnchor;
        Mesh mesh;
        MeshRenderer meshRenderer;
        MaterialPropertyBlock block;

        public bool IsEmitting { get; private set; }

        void Awake() => EnsureInitialised();

        void EnsureInitialised()
        {
            if (mesh != null)
                return;
            mesh = new Mesh { name = "BladeTrail" };
            mesh.MarkDynamic();
            GetComponent<MeshFilter>().sharedMesh = mesh;
            meshRenderer = GetComponent<MeshRenderer>();
            meshRenderer.shadowCastingMode = UnityEngine.Rendering.ShadowCastingMode.Off;
            meshRenderer.receiveShadows = false;
            block = new MaterialPropertyBlock();
        }

        public void Begin(Transform trailBase, Transform trailTip, Color tint, float sampleLifetime)
        {
            EnsureInitialised();
            baseAnchor = trailBase;
            tipAnchor = trailTip;
            lifetime = sampleLifetime;
            SetTint(tint);
            samples.Clear();
            IsEmitting = true;
        }

        public void End() => IsEmitting = false;

        public void Clear()
        {
            IsEmitting = false;
            samples.Clear();
            if (mesh != null)
                mesh.Clear();
        }

        public void SetTint(Color tint)
        {
            EnsureInitialised();
            block.SetColor(TintId, tint);
            meshRenderer.SetPropertyBlock(block);
        }

        /// <summary>Add a sample manually. Used by editor tooling that renders poses without Play mode.</summary>
        public void AddSample(Vector3 basePos, Vector3 tipPos, float time)
        {
            samples.Add(new Sample { basePos = basePos, tipPos = tipPos, time = time });
        }

        void LateUpdate()
        {
            float now = Time.time;
            if (IsEmitting && baseAnchor != null && tipAnchor != null)
                AddSample(baseAnchor.position, tipAnchor.position, now);
            Rebuild(now);
        }

        /// <summary>Drop expired samples and rebuild the ribbon as seen at time <paramref name="now"/>.</summary>
        public void Rebuild(float now)
        {
            EnsureInitialised();
            while (samples.Count > 0 && now - samples[0].time > lifetime)
                samples.RemoveAt(0);

            mesh.Clear();
            if (samples.Count < 2)
                return;

            vertices.Clear();
            colors.Clear();
            uvs.Clear();
            triangles.Clear();

            int last = samples.Count - 1;
            for (int i = 0; i < last; i++)
            {
                for (int s = 0; s < subdivisions; s++)
                    AppendPoint(i, s / (float)subdivisions, now);
            }
            AppendPoint(last, 0f, now);

            int pointCount = vertices.Count / 2;
            for (int p = 0; p < pointCount - 1; p++)
            {
                int b0 = p * 2, t0 = b0 + 1, b1 = b0 + 2, t1 = b0 + 3;
                triangles.Add(b0); triangles.Add(t0); triangles.Add(t1);
                triangles.Add(b0); triangles.Add(t1); triangles.Add(b1);
            }

            mesh.SetVertices(vertices);
            mesh.SetColors(colors);
            mesh.SetUVs(0, uvs);
            mesh.SetTriangles(triangles, 0);
            mesh.RecalculateBounds();
        }

        void AppendPoint(int index, float u, float now)
        {
            // Catmull-Rom through neighbouring samples; clamped at the ends.
            Sample p0 = samples[Mathf.Max(index - 1, 0)];
            Sample p1 = samples[index];
            Sample p2 = samples[Mathf.Min(index + 1, samples.Count - 1)];
            Sample p3 = samples[Mathf.Min(index + 2, samples.Count - 1)];

            Vector3 basePos = CatmullRom(p0.basePos, p1.basePos, p2.basePos, p3.basePos, u);
            Vector3 tipPos = CatmullRom(p0.tipPos, p1.tipPos, p2.tipPos, p3.tipPos, u);
            float time = Mathf.Lerp(p1.time, p2.time, u);

            float age = lifetime > 0f ? Mathf.Clamp01((now - time) / lifetime) : 1f;
            float fade = 1f - age;
            basePos = Vector3.Lerp(basePos, tipPos, age * taper);

            vertices.Add(basePos);
            vertices.Add(tipPos);
            colors.Add(new Color(1f, 1f, 1f, fade * fade));
            colors.Add(new Color(1f, 1f, 1f, fade));
            uvs.Add(new Vector2(age, 0f));
            uvs.Add(new Vector2(age, 1f));
        }

        static Vector3 CatmullRom(Vector3 p0, Vector3 p1, Vector3 p2, Vector3 p3, float t)
        {
            float t2 = t * t;
            float t3 = t2 * t;
            return 0.5f * (2f * p1 + (p2 - p0) * t
                + (2f * p0 - 5f * p1 + 4f * p2 - p3) * t2
                + (3f * p1 - p0 - 3f * p2 + p3) * t3);
        }

        void OnDestroy()
        {
            if (mesh == null)
                return;
            if (Application.isPlaying)
                Destroy(mesh);
            else
                DestroyImmediate(mesh);
        }
    }
}

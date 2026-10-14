using System.Collections.Generic;
using UnityEngine;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Builds faceted low-poly meshes from convex parts. Every part is convex, so each
    /// triangle's winding is fixed up by checking its normal against the part's centre,
    /// which removes a whole class of inside-out-face bugs.
    /// </summary>
    public class MeshBuilder
    {
        readonly List<Vector3> vertices = new();
        readonly List<Vector3> normals = new();
        readonly List<List<int>> submeshes = new();

        /// <summary>
        /// Loft a polygon along local +Y. Each ring is (y, width scale, depth scale).
        /// A ring with zero scale collapses to a point, which makes cones and blade tips.
        /// </summary>
        public MeshBuilder Loft(int submesh, Vector3 origin, Quaternion rotation, Vector2[] profile, params Vector3[] rings)
        {
            var points = new List<Vector3[]>();
            foreach (var ring in rings)
            {
                var ringPoints = new Vector3[profile.Length];
                for (int i = 0; i < profile.Length; i++)
                {
                    var local = new Vector3(profile[i].x * ring.y, ring.x, profile[i].y * ring.z);
                    ringPoints[i] = origin + rotation * local;
                }
                points.Add(ringPoints);
            }

            Vector3 centre = Vector3.zero;
            int count = 0;
            foreach (var ringPoints in points)
            {
                foreach (var p in ringPoints)
                {
                    centre += p;
                    count++;
                }
            }
            centre /= count;

            int n = profile.Length;
            for (int r = 0; r < points.Count - 1; r++)
            {
                for (int i = 0; i < n; i++)
                {
                    int j = (i + 1) % n;
                    Triangle(submesh, centre, points[r][i], points[r][j], points[r + 1][j]);
                    Triangle(submesh, centre, points[r][i], points[r + 1][j], points[r + 1][i]);
                }
            }

            Cap(submesh, centre, points[0]);
            Cap(submesh, centre, points[points.Count - 1]);
            return this;
        }

        /// <summary>Prism or cone along local +Y from <paramref name="origin"/>.</summary>
        public MeshBuilder Cylinder(int submesh, Vector3 origin, Quaternion rotation, int sides, float bottomRadius, float topRadius, float height)
        {
            return Loft(submesh, origin, rotation, Polygon(sides),
                new Vector3(0f, bottomRadius, bottomRadius),
                new Vector3(height, topRadius, topRadius));
        }

        public MeshBuilder Box(int submesh, Vector3 centre, Vector3 size, Quaternion rotation)
        {
            var square = new[] { new Vector2(-0.5f, -0.5f), new Vector2(0.5f, -0.5f), new Vector2(0.5f, 0.5f), new Vector2(-0.5f, 0.5f) };
            Vector3 origin = centre - rotation * new Vector3(0f, size.y * 0.5f, 0f);
            return Loft(submesh, origin, rotation, square,
                new Vector3(0f, size.x, size.z),
                new Vector3(size.y, size.x, size.z));
        }

        public MeshBuilder Box(int submesh, Vector3 centre, Vector3 size) => Box(submesh, centre, size, Quaternion.identity);

        /// <summary>Icosahedron, optionally subdivided once, scaled per axis. Reads as a faceted gem or joint.</summary>
        public MeshBuilder Ico(int submesh, Vector3 centre, float radius, Vector3 scale, bool subdivide = false)
        {
            float t = (1f + Mathf.Sqrt(5f)) / 2f;
            var v = new List<Vector3>
            {
                new(-1, t, 0), new(1, t, 0), new(-1, -t, 0), new(1, -t, 0),
                new(0, -1, t), new(0, 1, t), new(0, -1, -t), new(0, 1, -t),
                new(t, 0, -1), new(t, 0, 1), new(-t, 0, -1), new(-t, 0, 1),
            };
            var faces = new List<int[]>
            {
                new[] {0, 11, 5}, new[] {0, 5, 1}, new[] {0, 1, 7}, new[] {0, 7, 10}, new[] {0, 10, 11},
                new[] {1, 5, 9}, new[] {5, 11, 4}, new[] {11, 10, 2}, new[] {10, 7, 6}, new[] {7, 1, 8},
                new[] {3, 9, 4}, new[] {3, 4, 2}, new[] {3, 2, 6}, new[] {3, 6, 8}, new[] {3, 8, 9},
                new[] {4, 9, 5}, new[] {2, 4, 11}, new[] {6, 2, 10}, new[] {8, 6, 7}, new[] {9, 8, 1},
            };
            for (int i = 0; i < v.Count; i++)
                v[i] = v[i].normalized;

            if (subdivide)
            {
                var split = new List<int[]>();
                foreach (var f in faces)
                {
                    int a = Midpoint(v, f[0], f[1]), b = Midpoint(v, f[1], f[2]), c = Midpoint(v, f[2], f[0]);
                    split.Add(new[] { f[0], a, c });
                    split.Add(new[] { f[1], b, a });
                    split.Add(new[] { f[2], c, b });
                    split.Add(new[] { a, b, c });
                }
                faces = split;
            }

            foreach (var f in faces)
            {
                Triangle(submesh, centre,
                    centre + Vector3.Scale(v[f[0]], scale) * radius,
                    centre + Vector3.Scale(v[f[1]], scale) * radius,
                    centre + Vector3.Scale(v[f[2]], scale) * radius);
            }
            return this;
        }

        public MeshBuilder Ico(int submesh, Vector3 centre, float radius) => Ico(submesh, centre, radius, Vector3.one);

        public Mesh Build(string name)
        {
            var mesh = new Mesh { name = name };
            mesh.SetVertices(vertices);
            mesh.SetNormals(normals);
            mesh.subMeshCount = submeshes.Count;
            for (int i = 0; i < submeshes.Count; i++)
                mesh.SetTriangles(submeshes[i], i);
            mesh.RecalculateBounds();
            return mesh;
        }

        public static Vector2[] Polygon(int sides, float phaseDegrees = 0f)
        {
            var points = new Vector2[sides];
            for (int i = 0; i < sides; i++)
            {
                float a = (phaseDegrees + 360f * i / sides) * Mathf.Deg2Rad;
                points[i] = new Vector2(Mathf.Cos(a), Mathf.Sin(a));
            }
            return points;
        }

        /// <summary>Four-point diamond cross-section: x is the edge, z is the flat of the blade.</summary>
        public static readonly Vector2[] Diamond = { new(1, 0), new(0, 1), new(-1, 0), new(0, -1) };

        void Triangle(int submesh, Vector3 centre, Vector3 a, Vector3 b, Vector3 c)
        {
            Vector3 normal = Vector3.Cross(b - a, c - a);
            if (normal.sqrMagnitude < 1e-12f)
                return;
            normal.Normalize();

            // Unity treats clockwise triangles as front-facing, whose cross product points at the viewer.
            if (Vector3.Dot(normal, (a + b + c) / 3f - centre) < 0f)
            {
                (b, c) = (c, b);
                normal = -normal;
            }

            while (submeshes.Count <= submesh)
                submeshes.Add(new List<int>());

            int start = vertices.Count;
            vertices.Add(a);
            vertices.Add(b);
            vertices.Add(c);
            normals.Add(normal);
            normals.Add(normal);
            normals.Add(normal);
            submeshes[submesh].Add(start);
            submeshes[submesh].Add(start + 1);
            submeshes[submesh].Add(start + 2);
        }

        void Cap(int submesh, Vector3 centre, Vector3[] ring)
        {
            Vector3 middle = Vector3.zero;
            foreach (var p in ring)
                middle += p;
            middle /= ring.Length;
            for (int i = 0; i < ring.Length; i++)
                Triangle(submesh, centre, middle, ring[i], ring[(i + 1) % ring.Length]);
        }

        static int Midpoint(List<Vector3> v, int a, int b)
        {
            v.Add(((v[a] + v[b]) * 0.5f).normalized);
            return v.Count - 1;
        }
    }
}

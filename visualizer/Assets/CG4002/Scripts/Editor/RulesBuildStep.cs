using System.IO;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Keeps Resources/Rules/weapons.txt and match.txt (committed) in step with the engine's
    /// shared/config/weapons.yaml and match.yaml before every build and on editor load, so the demo
    /// engine (Offline/LocalGameRules, via GameRules) plays by the engine's current damage, range and
    /// cooldowns. On a checkout without shared/ the committed copies are used as they are.
    /// </summary>
    [InitializeOnLoad]
    public class RulesBuildStep : IPreprocessBuildWithReport
    {
        // Relative to the Unity project folder (CG4002/visualizer).
        static readonly (string source, string destination)[] Files =
        {
            ("../shared/config/weapons.yaml", "Assets/CG4002/Resources/Rules/weapons.txt"),
            ("../shared/config/match.yaml", "Assets/CG4002/Resources/Rules/match.txt"),
        };

        public int callbackOrder => 0;

        static RulesBuildStep() => EditorApplication.delayCall += () => Refresh(false);

        public void OnPreprocessBuild(BuildReport report) => Refresh(true);

        [MenuItem("CG4002/Integration/Refresh Game Rules From shared config")]
        static void RefreshFromMenu() => Refresh(true);

        static void Refresh(bool loud)
        {
            foreach (var (source, destination) in Files)
            {
                if (!File.Exists(source))
                {
                    if (loud)
                        Debug.LogWarning($"No {source}: the demo uses the rules last copied into {destination}.");
                    continue;
                }
                string text = File.ReadAllText(source);
                if (File.Exists(destination) && File.ReadAllText(destination) == text)
                    continue;
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                File.WriteAllText(destination, text);
                AssetDatabase.ImportAsset(destination);
                Debug.Log($"Game rules copied from {source} into the build.");
            }
        }
    }
}

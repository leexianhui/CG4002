using System.IO;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace CG4002.EditorTools
{
    /// <summary>
    /// Keeps Resources/engine_token.txt (committed) in step with comms/.env.engine before every build
    /// and on editor load, so the phone app fills the token in by itself. On a checkout without the
    /// env files the committed copy is used as is.
    /// </summary>
    [InitializeOnLoad]
    public class EngineTokenBuildStep : IPreprocessBuildWithReport
    {
        // Relative to the Unity project folder (CG4002/visualizer). First file with a token wins.
        static readonly string[] Sources = { "../comms/.env.engine", "../comms/.env.relay", "../comms/.env" };
        static readonly Regex TokenLine = new Regex(
            @"^\s*(?:CG_ENGINE_BEARER_TOKEN|CG_RELAY_ENGINE_LINK__BEARER_TOKEN)\s*=\s*""?([^""#\s]+)",
            RegexOptions.Multiline);
        const string Destination = "Assets/CG4002/Resources/engine_token.txt";

        public int callbackOrder => 0;

        static EngineTokenBuildStep() => EditorApplication.delayCall += () => Refresh(false);

        public void OnPreprocessBuild(BuildReport report) => Refresh(true);

        [MenuItem("CG4002/Integration/Refresh Engine Token From comms .env")]
        static void RefreshFromMenu() => Refresh(true);

        static void Refresh(bool loud)
        {
            string token = null, from = null;
            foreach (var path in Sources)
            {
                if (!File.Exists(path)) continue;
                var match = TokenLine.Match(File.ReadAllText(path));
                if (match.Success) { token = match.Groups[1].Value; from = path; break; }
            }
            if (token == null)
            {
                if (loud)
                    Debug.LogWarning("No engine bearer token found in comms/.env.engine, .env.relay or .env: " +
                        "the app will ask for it on the phone.");
                return;
            }
            if (File.Exists(Destination) && File.ReadAllText(Destination).Trim() == token)
            {
                if (loud) Debug.Log("Engine token is up to date (from " + from + ").");
                return;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(Destination));
            File.WriteAllText(Destination, token);
            AssetDatabase.ImportAsset(Destination);
            Debug.Log("Engine token copied from " + from + " into the build.");
        }
    }
}

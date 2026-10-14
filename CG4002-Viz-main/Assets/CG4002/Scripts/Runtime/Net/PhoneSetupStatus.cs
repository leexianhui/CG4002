using System;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace CG4002.Net
{
    /// <summary>Read-only setup information from the latest complete engine snapshot.</summary>
    public sealed class PhoneSetupStatus
    {
        public sealed class Player
        {
            public bool relayConnected, ready, hasPosition;
            public Vector2 position;
            public float heading;
        }

        public readonly Player[] players = { new(), new() };
        public string phase;
        public string reason;
        public float? Separation => players[0].hasPosition && players[1].hasPosition
            ? Vector2.Distance(players[0].position, players[1].position) : null;

        // Called only after EngineWireCodec has validated the whole snapshot.
        public static PhoneSetupStatus Read(string json)
        {
            var root = JObject.Parse(json);
            var result = new PhoneSetupStatus { phase = (string)root["phase"], reason = (string)root["reason"] };
            foreach (var item in (JArray)root["players"])
            {
                int index = (int)item["playerId"] - 1;
                if (index < 0 || index > 1) throw new FormatException("Invalid player");
                var p = result.players[index];
                p.relayConnected = (bool)item["connected"];
                p.ready = (bool)item["ready"];
                if (item["position"] is JObject position)
                {
                    p.hasPosition = true;
                    p.position = new Vector2((float)position["x"], (float)position["y"]);
                    p.heading = (float)position["heading"];
                }
            }
            return result;
        }

        public string PlayerLine(int index)
        {
            var p = players[index];
            string pose = p.hasPosition
                ? $"x {p.position.x:F2}, y {p.position.y:F2} m, {p.heading:F0} deg"
                : "position unavailable";
            return $"P{index + 1}: relay {(p.relayConnected ? "on" : "off")}, " +
                $"{(p.ready ? "ready" : "not ready")}\n{pose}";
        }
    }
}

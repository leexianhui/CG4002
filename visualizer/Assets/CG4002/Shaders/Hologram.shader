// Translucent "hologram" for the weapon-selection dummies: a faint fill that brightens toward the
// silhouette, plus a solid outline. The outline is the classic inverted hull: back faces pushed out
// along the normal, hidden by the fill's depth everywhere except around the edge.
// URP draws a renderer's SRPDefaultUnlit pass before its UniversalForward pass, so the fill (which
// must write depth first) uses SRPDefaultUnlit and the outline UniversalForward.
// _Color is set per dummy through a MaterialPropertyBlock (the weapon's accent colour).
Shader "CG4002/Hologram"
{
    Properties
    {
        [HDR] _Color ("Colour", Color) = (0.4, 1.3, 1.5, 1)
        _FillAlpha ("Fill Alpha", Range(0, 1)) = 0.12
        _RimPower ("Rim Power", Range(0.5, 8)) = 2.5
        _OutlineWidth ("Outline Width (m)", Range(0, 0.05)) = 0.012
    }

    SubShader
    {
        Tags
        {
            "RenderPipeline" = "UniversalPipeline"
            "RenderType" = "Transparent"
            "Queue" = "Transparent"
            "IgnoreProjector" = "True"
        }

        HLSLINCLUDE
        #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

        CBUFFER_START(UnityPerMaterial)
            half4 _Color;
            half _FillAlpha;
            half _RimPower;
            float _OutlineWidth;
        CBUFFER_END
        ENDHLSL

        // Drawn first: writes depth so the outline only shows around the silhouette.
        Pass
        {
            Name "Fill"
            Tags { "LightMode" = "SRPDefaultUnlit" }
            Blend SrcAlpha OneMinusSrcAlpha
            ZWrite On
            Cull Back

            HLSLPROGRAM
            #pragma vertex Vert
            #pragma fragment Frag

            struct Attributes
            {
                float4 positionOS : POSITION;
                float3 normalOS : NORMAL;
            };

            struct Varyings
            {
                float4 positionCS : SV_POSITION;
                float3 normalWS : TEXCOORD0;
                float3 viewWS : TEXCOORD1;
            };

            Varyings Vert(Attributes input)
            {
                Varyings output;
                float3 positionWS = TransformObjectToWorld(input.positionOS.xyz);
                output.positionCS = TransformWorldToHClip(positionWS);
                output.normalWS = TransformObjectToWorldNormal(input.normalOS);
                output.viewWS = GetWorldSpaceViewDir(positionWS);
                return output;
            }

            half4 Frag(Varyings input) : SV_Target
            {
                half facing = saturate(dot(normalize(input.normalWS), normalize(input.viewWS)));
                half rim = pow(1.0h - facing, _RimPower);
                return half4(_Color.rgb, saturate(_FillAlpha + rim * (1.0h - _FillAlpha)));
            }
            ENDHLSL
        }

        Pass
        {
            Name "Outline"
            Tags { "LightMode" = "UniversalForward" }
            Blend SrcAlpha OneMinusSrcAlpha
            ZWrite Off
            Cull Front

            HLSLPROGRAM
            #pragma vertex Vert
            #pragma fragment Frag

            struct Attributes
            {
                float4 positionOS : POSITION;
                float3 normalOS : NORMAL;
            };

            float4 Vert(Attributes input) : SV_POSITION
            {
                float3 positionWS = TransformObjectToWorld(input.positionOS.xyz);
                float3 normalWS = TransformObjectToWorldNormal(input.normalOS);
                return TransformWorldToHClip(positionWS + normalWS * _OutlineWidth);
            }

            half4 Frag() : SV_Target
            {
                return half4(_Color.rgb, 1.0h);
            }
            ENDHLSL
        }
    }
}

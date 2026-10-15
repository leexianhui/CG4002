// Unlit additive shader for trails and particles.
// Vertex colour carries per-vertex fade; _TintColor carries HDR intensity so bloom picks it up.
// _Radial = 0: trail mode, brightest along the outer edge (uv.y = 1 at the blade tip).
// _Radial = 1: soft round sprite for particles.
Shader "CG4002/FX Additive"
{
    Properties
    {
        [HDR] _TintColor ("Tint", Color) = (1, 1, 1, 1)
        [Toggle] _Radial ("Radial Falloff", Float) = 0
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

        Blend SrcAlpha One
        ZWrite Off
        Cull Off

        Pass
        {
            Name "FXAdditive"
            Tags { "LightMode" = "UniversalForward" }

            HLSLPROGRAM
            #pragma vertex Vert
            #pragma fragment Frag

            #include "Packages/com.unity.render-pipelines.universal/ShaderLibrary/Core.hlsl"

            CBUFFER_START(UnityPerMaterial)
                half4 _TintColor;
                half _Radial;
            CBUFFER_END

            struct Attributes
            {
                float4 positionOS : POSITION;
                half4 color : COLOR;
                float2 uv : TEXCOORD0;
            };

            struct Varyings
            {
                float4 positionCS : SV_POSITION;
                half4 color : COLOR;
                float2 uv : TEXCOORD0;
            };

            Varyings Vert(Attributes input)
            {
                Varyings output;
                output.positionCS = TransformObjectToHClip(input.positionOS.xyz);
                output.color = input.color;
                output.uv = input.uv;
                return output;
            }

            half4 Frag(Varyings input) : SV_Target
            {
                half trailMask = input.uv.y * input.uv.y;
                half radialMask = saturate(1.0 - length(input.uv * 2.0 - 1.0));
                half mask = lerp(trailMask, radialMask * radialMask, _Radial);
                return half4(input.color.rgb * _TintColor.rgb, input.color.a * mask);
            }
            ENDHLSL
        }
    }
}

//!PARAM exposure
//!DESC Exposure in stops
//!TYPE float
//!MINIMUM -6.0
//!MAXIMUM 6.0
0.0

//!HOOK MAIN
//!BIND HOOKED
//!DESC Panasonic V-Log scene-linear exposure before display LUT

float vlog_to_linear_channel(float v)
{
    const float cut = 0.181;
    const float b = 0.00873;
    const float c = 0.241514;
    const float d = 0.598206;
    return v < cut ? (v - 0.125) / 5.6 : pow(10.0, (v - d) / c) - b;
}

float linear_to_vlog_channel(float x)
{
    const float cut = 0.01;
    const float b = 0.00873;
    const float c = 0.241514;
    const float d = 0.598206;
    return x < cut ? 5.6 * x + 0.125 : c * (log(x + b) / log(10.0)) + d;
}

vec3 vlog_to_linear(vec3 v)
{
    return vec3(
        vlog_to_linear_channel(v.r),
        vlog_to_linear_channel(v.g),
        vlog_to_linear_channel(v.b)
    );
}

vec3 linear_to_vlog(vec3 x)
{
    return vec3(
        linear_to_vlog_channel(x.r),
        linear_to_vlog_channel(x.g),
        linear_to_vlog_channel(x.b)
    );
}

vec4 hook()
{
    vec4 src = HOOKED_texOff(0);
    vec3 linear = vlog_to_linear(clamp(src.rgb, 0.0, 1.0));
    linear *= exp2(exposure);
    vec3 encoded = linear_to_vlog(max(linear, vec3(-0.0223214286)));
    return vec4(clamp(encoded, 0.0, 1.0), src.a);
}

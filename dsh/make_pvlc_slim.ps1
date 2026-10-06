# make_pvlc_slim.ps1
# Build pvlc_slim\ from pvlc\, keeping only what the player needs.
#
# READ-ONLY with respect to pvlc\: this script never deletes or writes anything
# inside pvlc\. It only reads pvlc\ and writes a fresh pvlc_slim\.
#
# Lives in dsh\; the project root (holding pvlc\ and pvlc_slim\) is one level up.
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 as ANSI
# (GBK on this box), so UTF-8 Chinese comments get mangled and can even swallow
# the newline after them, destroying the whitelist. That bug already bit once.

param([string]$Root = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
$root = $Root
$src  = Join-Path $root 'pvlc'
$dst  = Join-Path $root 'pvlc_slim'

if (-not (Test-Path -LiteralPath $src)) { throw "missing source: $src" }

# ---- top level ----
$topKeep = @('libvlc.dll', 'libvlccore.dll')

# ---- plugin files to keep, relative to pvlc\plugins\ ----
# Dropped on purpose: gui/skins2, transcoders (x264/x265/vpx/aom), stream output,
# visualization, service discovery, dev sdk, plus access/demux modules for
# optical discs and protocols this player never uses.
$pluginKeep = @(
    # access: local file / mrl / basic network input
    'access\libfilesystem_plugin.dll'
    'access\libimem_plugin.dll'
    'access\libidummy_plugin.dll'
    'access\libaccess_imem_plugin.dll'
    'access\libattachment_plugin.dll'
    'access\libhttp_plugin.dll'
    'access\libhttps_plugin.dll'
    'access\libaccess_mms_plugin.dll'
    'access\libaccess_concat_plugin.dll'
    'access\libaccess_wasapi_plugin.dll'
    'access\libtcp_plugin.dll'
    'access\libudp_plugin.dll'
    'access\libsdp_plugin.dll'
    'access\libtimecode_plugin.dll'
    'access\libshm_plugin.dll'
    # access_output (player does not stream, kept minimal)
    'access_output\libaccess_output_dummy_plugin.dll'
    'access_output\libaccess_output_file_plugin.dll'
    'access_output\libaccess_output_http_plugin.dll'
    'access_output\libaccess_output_udp_plugin.dll'
    # audio_filter: format conversion, tempo/quality helpers
    'audio_filter\libaudio_format_plugin.dll'
    'audio_filter\libequalizer_plugin.dll'
    'audio_filter\libgain_plugin.dll'
    'audio_filter\libcompressor_plugin.dll'
    'audio_filter\libnormvol_plugin.dll'
    'audio_filter\libparam_eq_plugin.dll'
    'audio_filter\libscaletempo_plugin.dll'
    'audio_filter\libscaletempo_pitch_plugin.dll'
    'audio_filter\libsamplerate_plugin.dll'
    'audio_filter\libspeex_resampler_plugin.dll'
    'audio_filter\libugly_resampler_plugin.dll'
    'audio_filter\libmad_plugin.dll'
    'audio_filter\libremap_plugin.dll'
    'audio_filter\libtrivial_channel_mixer_plugin.dll'
    'audio_filter\libsimple_channel_mixer_plugin.dll'
    'audio_filter\libheadphone_channel_mixer_plugin.dll'
    'audio_filter\libmono_plugin.dll'
    'audio_filter\libstereo_widen_plugin.dll'
    'audio_filter\libspatialaudio_plugin.dll'
    'audio_filter\libtospdif_plugin.dll'
    'audio_filter\libdolby_surround_decoder_plugin.dll'
    # audio_mixer
    'audio_mixer\libfloat_mixer_plugin.dll'
    'audio_mixer\libinteger_mixer_plugin.dll'
    # audio_output: Windows backends (mmdevice is the default)
    'audio_output\libmmdevice_plugin.dll'
    'audio_output\libwasapi_plugin.dll'
    'audio_output\libdirectsound_plugin.dll'
    'audio_output\libwaveout_plugin.dll'
    'audio_output\libamem_plugin.dll'
    # codec: decoders, subtitles, image codecs
    'codec\libavcodec_plugin.dll'
    'codec\liba52_plugin.dll'
    'codec\libadpcm_plugin.dll'
    'codec\libaraw_plugin.dll'
    'codec\libcc_plugin.dll'
    'codec\libcvdsub_plugin.dll'
    'codec\libd3d11va_plugin.dll'
    'codec\libddummy_plugin.dll'
    'codec\libdmo_plugin.dll'
    'codec\libdvbsub_plugin.dll'
    'codec\libdxva2_plugin.dll'
    'codec\libedummy_plugin.dll'
    'codec\libfaad_plugin.dll'
    'codec\libflac_plugin.dll'
    'codec\libg711_plugin.dll'
    'codec\libjpeg_plugin.dll'
    'codec\libkate_plugin.dll'
    'codec\liblibass_plugin.dll'
    'codec\liblibmpeg2_plugin.dll'
    'codec\liblpcm_plugin.dll'
    'codec\libmft_plugin.dll'
    'codec\libmpg123_plugin.dll'
    'codec\libopus_plugin.dll'
    'codec\libpng_plugin.dll'
    'codec\librawvideo_plugin.dll'
    'codec\librtpvideo_plugin.dll'
    'codec\libscte18_plugin.dll'
    'codec\libscte27_plugin.dll'
    'codec\libspdif_plugin.dll'
    'codec\libspeex_plugin.dll'
    'codec\libspudec_plugin.dll'
    'codec\libstl_plugin.dll'
    'codec\libsubsdec_plugin.dll'
    'codec\libsubstx3g_plugin.dll'
    'codec\libsubsusf_plugin.dll'
    'codec\libsvcdsub_plugin.dll'
    'codec\libt140_plugin.dll'
    'codec\libtextst_plugin.dll'
    'codec\libtheora_plugin.dll'
    'codec\libttml_plugin.dll'
    'codec\libuleaddvaudio_plugin.dll'
    'codec\libvorbis_plugin.dll'
    'codec\libwebvtt_plugin.dll'
    # control: window/hotkey plumbing
    'control\libdummy_plugin.dll'
    'control\libgestures_plugin.dll'
    'control\libhotkeys_plugin.dll'
    'control\libwin_hotkeys_plugin.dll'
    'control\libwin_msg_plugin.dll'
    # d3d filters used by d3d11/d3d9 video output
    'd3d11\libdirect3d11_filters_plugin.dll'
    'd3d9\libdirect3d9_filters_plugin.dll'
    # demux: containers and audio formats this player opens
    'demux\libaiff_plugin.dll'
    'demux\libasf_plugin.dll'
    'demux\libau_plugin.dll'
    'demux\libavi_plugin.dll'
    'demux\libcaf_plugin.dll'
    'demux\libdemuxdump_plugin.dll'
    'demux\libdirectory_demux_plugin.dll'
    'demux\libes_plugin.dll'
    'demux\libflacsys_plugin.dll'
    'demux\libh26x_plugin.dll'
    'demux\libimage_plugin.dll'
    'demux\libmjpeg_plugin.dll'
    'demux\libmkv_plugin.dll'
    'demux\libmp4_plugin.dll'
    'demux\libmpc_plugin.dll'
    'demux\libmpgv_plugin.dll'
    'demux\libnoseek_plugin.dll'
    'demux\libnsv_plugin.dll'
    'demux\libnuv_plugin.dll'
    'demux\libogg_plugin.dll'
    'demux\libplaylist_plugin.dll'
    'demux\libps_plugin.dll'
    'demux\libpva_plugin.dll'
    'demux\librawaud_plugin.dll'
    'demux\librawdv_plugin.dll'
    'demux\librawvid_plugin.dll'
    'demux\libreal_plugin.dll'
    'demux\libsmf_plugin.dll'
    'demux\libsubtitle_plugin.dll'
    'demux\libts_plugin.dll'
    'demux\libtta_plugin.dll'
    'demux\libty_plugin.dll'
    'demux\libvc1_plugin.dll'
    'demux\libvobsub_plugin.dll'
    'demux\libvoc_plugin.dll'
    'demux\libwav_plugin.dll'
    'demux\libxa_plugin.dll'
    # keystore / logger
    'keystore\libfile_keystore_plugin.dll'
    'keystore\libmemory_keystore_plugin.dll'
    'logger\libconsole_logger_plugin.dll'
    'logger\libfile_logger_plugin.dll'
    # misc: TLS and rtsp vod support
    'misc\libgnutls_plugin.dll'
    'misc\libvod_rtsp_plugin.dll'
    # mux: only what record/transcode plumbing references
    'mux\libmux_dummy_plugin.dll'
    'mux\libmux_mp4_plugin.dll'
    'mux\libmux_ogg_plugin.dll'
    'mux\libmux_ts_plugin.dll'
    'mux\libmux_wav_plugin.dll'
    # packetizer: needed by network and ts inputs
    'packetizer\libpacketizer_a52_plugin.dll'
    'packetizer\libpacketizer_av1_plugin.dll'
    'packetizer\libpacketizer_copy_plugin.dll'
    'packetizer\libpacketizer_dts_plugin.dll'
    'packetizer\libpacketizer_flac_plugin.dll'
    'packetizer\libpacketizer_h264_plugin.dll'
    'packetizer\libpacketizer_hevc_plugin.dll'
    'packetizer\libpacketizer_mlp_plugin.dll'
    'packetizer\libpacketizer_mpeg4audio_plugin.dll'
    'packetizer\libpacketizer_mpeg4video_plugin.dll'
    'packetizer\libpacketizer_mpegaudio_plugin.dll'
    'packetizer\libpacketizer_mpegvideo_plugin.dll'
    'packetizer\libpacketizer_vc1_plugin.dll'
    # spu: subtitle/marquee/logo overlay. marq and logo are option providers:
    # dropping them makes VLC log "option marq-* does not exist" at startup.
    'spu\libmosaic_plugin.dll'
    'spu\libmarq_plugin.dll'
    'spu\liblogo_plugin.dll'
    'spu\libsubsdelay_plugin.dll'
    'spu\libaudiobargraph_v_plugin.dll'
    # stream_filter: caching (network) and record
    'stream_filter\libcache_read_plugin.dll'
    'stream_filter\libcache_block_plugin.dll'
    'stream_filter\libprefetch_plugin.dll'
    'stream_filter\librecord_plugin.dll'
    'stream_filter\libinflate_plugin.dll'
    'stream_filter\libskiptags_plugin.dll'
    # text_renderer
    'text_renderer\libfreetype_plugin.dll'
    'text_renderer\libtdummy_plugin.dll'
    'text_renderer\libsapi_plugin.dll'
    # video_chroma: colour conversions
    'video_chroma\libchain_plugin.dll'
    'video_chroma\libgrey_yuv_plugin.dll'
    'video_chroma\libi420_10_p010_plugin.dll'
    'video_chroma\libi420_nv12_plugin.dll'
    'video_chroma\libi420_rgb_mmx_plugin.dll'
    'video_chroma\libi420_rgb_plugin.dll'
    'video_chroma\libi420_rgb_sse2_plugin.dll'
    'video_chroma\libi420_yuy2_mmx_plugin.dll'
    'video_chroma\libi420_yuy2_plugin.dll'
    'video_chroma\libi420_yuy2_sse2_plugin.dll'
    'video_chroma\libi422_i420_plugin.dll'
    'video_chroma\libi422_yuy2_mmx_plugin.dll'
    'video_chroma\libi422_yuy2_plugin.dll'
    'video_chroma\libi422_yuy2_sse2_plugin.dll'
    'video_chroma\librv32_plugin.dll'
    'video_chroma\libswscale_plugin.dll'
    'video_chroma\libyuvp_plugin.dll'
    'video_chroma\libyuy2_i420_plugin.dll'
    'video_chroma\libyuy2_i422_plugin.dll'
    # video_filter: deinterlace, scale, orientation
    'video_filter\libdeinterlace_plugin.dll'
    'video_filter\libscale_plugin.dll'
    'video_filter\libtransform_plugin.dll'
    'video_filter\libcroppadd_plugin.dll'
    'video_filter\libadjust_plugin.dll'
    'video_filter\libantiflicker_plugin.dll'
    'video_filter\libfps_plugin.dll'
    'video_filter\libpostproc_plugin.dll'
    'video_filter\libcanvas_plugin.dll'
    'video_filter\libextract_plugin.dll'
    # video_output: direct3d family, used by MV mode via set_hwnd.
    # vmem is an option provider too.
    'video_output\libdirect3d11_plugin.dll'
    'video_output\libdirect3d9_plugin.dll'
    'video_output\libdirectdraw_plugin.dll'
    'video_output\libdrawable_plugin.dll'
    'video_output\libwingdi_plugin.dll'
    'video_output\libglwin32_plugin.dll'
    'video_output\libvdummy_plugin.dll'
    'video_output\libwinhibit_plugin.dll'
    'video_output\libvmem_plugin.dll'
)

$pluginKeep = $pluginKeep | Sort-Object -Unique

# ---- refuse to build if the whitelist does not match the source ----
$missing = @()
foreach ($f in $topKeep) {
    if (-not (Test-Path -LiteralPath (Join-Path $src $f))) { $missing += $f }
}
foreach ($f in $pluginKeep) {
    if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $src 'plugins') $f))) {
        $missing += "plugins\$f"
    }
}
if ($missing.Count -gt 0) {
    Write-Host 'KEEP LIST HAS ENTRIES MISSING FROM SOURCE:'
    $missing | ForEach-Object { Write-Host "  $_" }
    throw 'refusing to build: keep list does not match source'
}

# ---- build ----
if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Recurse -Force }
New-Item -ItemType Directory -Path $dst | Out-Null
New-Item -ItemType Directory -Path (Join-Path $dst 'plugins') | Out-Null

$copied = 0
foreach ($f in $topKeep) {
    Copy-Item -LiteralPath (Join-Path $src $f) -Destination (Join-Path $dst $f) -Force
    $copied++
}
foreach ($f in $pluginKeep) {
    $target = Join-Path (Join-Path $dst 'plugins') $f
    $dir = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Copy-Item -LiteralPath (Join-Path (Join-Path $src 'plugins') $f) -Destination $target -Force
    $copied++
}

$srcFiles = Get-ChildItem $src -Recurse -File
$dstFiles = Get-ChildItem $dst -Recurse -File
$srcMB = ($srcFiles | Measure-Object Length -Sum).Sum / 1MB
$dstMB = ($dstFiles | Measure-Object Length -Sum).Sum / 1MB

Write-Host ''
Write-Host ("source pvlc      : {0,4} files  {1,8:N1} MB" -f $srcFiles.Count, $srcMB)
Write-Host ("output pvlc_slim : {0,4} files  {1,8:N1} MB" -f $dstFiles.Count, $dstMB)
Write-Host ("kept             : {0,4} files" -f $copied)
Write-Host ("dropped          : {0,4} files  {1,8:N1} MB  ({2:N0}% of size)" -f ($srcFiles.Count - $dstFiles.Count), ($srcMB - $dstMB), (100 * ($srcMB - $dstMB) / $srcMB))
Write-Host ''
Write-Host "output dir: $dst"

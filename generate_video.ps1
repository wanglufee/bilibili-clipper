# B站直播精彩片段自动生成脚本 (PowerShell版本)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "B站直播精彩片段自动生成脚本" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 步骤1: 检查环境
Write-Host "步骤1: 检查环境" -ForegroundColor Yellow

# 检查FFmpeg
$ffmpegPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpegPath) {
    Write-Host "❌ FFmpeg未安装" -ForegroundColor Red
    Write-Host "请先安装FFmpeg:" -ForegroundColor Yellow
    Write-Host "1. 访问 https://ffmpeg.org/download.html" -ForegroundColor Yellow
    Write-Host "2. 或运行: choco install ffmpeg -y (需要管理员权限)" -ForegroundColor Yellow
    pause
    exit 1
}
Write-Host "✅ FFmpeg已安装" -ForegroundColor Green

# 检查yt-dlp
$ytdlpPath = Get-Command yt-dlp -ErrorAction SilentlyContinue
if (-not $ytdlpPath) {
    Write-Host "❌ yt-dlp未安装" -ForegroundColor Red
    Write-Host "正在安装yt-dlp..." -ForegroundColor Yellow
    pip install yt-dlp
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ yt-dlp安装失败" -ForegroundColor Red
        pause
        exit 1
    }
}
Write-Host "✅ yt-dlp已安装" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "步骤2: 下载视频" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$videoUrl = "https://www.bilibili.com/video/BV1Mgd6BPEH8"
$outputFile = "bilibili-clipper\download\source_video.mp4"

Write-Host "下载视频: $videoUrl" -ForegroundColor Yellow
Write-Host "输出文件: $outputFile" -ForegroundColor Yellow
Write-Host ""

if (Test-Path $outputFile) {
    Write-Host "⚠️ 视频文件已存在，跳过下载" -ForegroundColor Yellow
} else {
    # 创建下载目录
    $downloadDir = Split-Path $outputFile -Parent
    if (-not (Test-Path $downloadDir)) {
        New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    }
    
    Write-Host "开始下载视频..." -ForegroundColor Yellow
    yt-dlp -f "best[height<=1080]" $videoUrl -o $outputFile
    
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 视频下载失败" -ForegroundColor Red
        pause
        exit 1
    }
    Write-Host "✅ 视频下载完成" -ForegroundColor Green
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "步骤3: 剪辑精彩片段" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$clipsDir = "bilibili-clipper\clips"
if (-not (Test-Path $clipsDir)) {
    New-Item -ItemType Directory -Path $clipsDir -Force | Out-Null
}

Write-Host "基于弹幕分析，剪辑以下精彩片段:" -ForegroundColor Yellow
Write-Host "1. 17:20-17:30 (1040-1050秒) - '雨哥牛逼'出现7次" -ForegroundColor White
Write-Host "2. 09:40-09:50 (580-590秒)   - '雨哥牛逼'出现5次" -ForegroundColor White
Write-Host "3. 17:10-17:20 (1030-1040秒) - '雨哥牛逼'出现4次" -ForegroundColor White
Write-Host "4. 15:30-15:40 (930-940秒)   - '雨哥牛逼'出现4次" -ForegroundColor White
Write-Host "5. 10:10-10:20 (610-620秒)   - '雨哥牛逼'出现4次" -ForegroundColor White
Write-Host ""

Write-Host "开始剪辑..." -ForegroundColor Yellow

# 定义剪辑片段
$clips = @(
    @{Name="clip_01.mp4"; Start=1040; Duration=10; Desc="17:20-17:30"},
    @{Name="clip_02.mp4"; Start=580; Duration=10; Desc="09:40-09:50"},
    @{Name="clip_03.mp4"; Start=1030; Duration=10; Desc="17:10-17:20"},
    @{Name="clip_04.mp4"; Start=930; Duration=10; Desc="15:30-15:40"},
    @{Name="clip_05.mp4"; Start=610; Duration=10; Desc="10:10-10:20"}
)

foreach ($clip in $clips) {
    $outputPath = Join-Path $clipsDir $clip.Name
    Write-Host "剪辑: $($clip.Desc)..." -NoNewline
    
    $ffmpegCmd = "ffmpeg -i `"$outputFile`" -ss $($clip.Start) -t $($clip.Duration) -c copy `"$outputPath`" -y -loglevel error"
    cmd /c $ffmpegCmd 2>$null
    
    if ($LASTEXITCODE -eq 0) {
        Write-Host " ✅" -ForegroundColor Green
    } else {
        Write-Host " ❌" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "步骤4: 合并片段" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$finalClip = Join-Path $clipsDir "final_highlights.mp4"

Write-Host "创建文件列表..." -ForegroundColor Yellow
$filelistContent = @()
foreach ($clip in $clips) {
    $filelistContent += "file '$($clip.Name)'"
}
$filelistContent | Out-File -FilePath (Join-Path $clipsDir "filelist.txt") -Encoding UTF8

Write-Host "合并所有片段..." -ForegroundColor Yellow
$concatCmd = "ffmpeg -f concat -safe 0 -i `"$(Join-Path $clipsDir 'filelist.txt')`" -c copy `"$finalClip`" -y -loglevel error"
cmd /c $concatCmd 2>$null

if ($LASTEXITCODE -eq 0) {
    Write-Host "✅ 合并完成: $finalClip" -ForegroundColor Green
} else {
    Write-Host "❌ 合并失败" -ForegroundColor Red
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "步骤5: 生成报告" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$reportFile = Join-Path $clipsDir "generation_report.txt"

$reportContent = @"
B站直播精彩片段生成报告
=======================
生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

源视频: $outputFile
精彩片段目录: $clipsDir

生成的片段:
1. clip_01.mp4 - 17:20-17:30 (10秒)
   理由: "雨哥牛逼"出现7次

2. clip_02.mp4 - 09:40-09:50 (10秒)
   理由: "雨哥牛逼"出现5次

3. clip_03.mp4 - 17:10-17:20 (10秒)
   理由: "雨哥牛逼"出现4次

4. clip_04.mp4 - 15:30-15:40 (10秒)
   理由: "雨哥牛逼"出现4次

5. clip_05.mp4 - 10:10-10:20 (10秒)
   理由: "雨哥牛逼"出现4次

最终集锦: final_highlights.mp4 (50秒)

文件大小:
"@

$reportContent | Out-File -FilePath $reportFile -Encoding UTF8

# 添加文件大小信息
Get-ChildItem $clipsDir -Filter *.mp4 | ForEach-Object {
    $sizeMB = [math]::Round($_.Length / 1MB, 2)
    "  $($_.Name) - $sizeMB MB" | Out-File -FilePath $reportFile -Encoding UTF8 -Append
}

Write-Host "✅ 报告生成: $reportFile" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "生成的视频文件:" -ForegroundColor Yellow
Get-ChildItem $clipsDir -Filter *.mp4 | ForEach-Object {
    Write-Host "  $($_.Name)" -ForegroundColor White
}

Write-Host ""
Write-Host "请检查 $clipsDir 目录查看生成的视频" -ForegroundColor Yellow
Write-Host ""
pause
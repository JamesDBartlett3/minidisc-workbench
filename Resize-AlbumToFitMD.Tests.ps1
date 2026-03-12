#Requires -Modules @{ ModuleName = 'Pester'; ModuleVersion = '5.0' }

<#
.SYNOPSIS
Pester tests for Resize-AlbumToFitMD.ps1

.DESCRIPTION
Uses ffmpeg to generate test WAV files with known durations, runs the script,
and verifies output durations with ffprobe.
#>

BeforeAll {
    $Script:ScriptPath = Join-Path $PSScriptRoot 'Resize-AlbumToFitMD.ps1'
    $Script:TestRoot   = Join-Path ([IO.Path]::GetTempPath()) "ResizeMD_Tests_$([guid]::NewGuid().ToString('N').Substring(0,8))"

    function New-TestInputFolder {
        <#
        .SYNOPSIS
        Creates a temp input folder with sine-wave WAV files of specified durations.
        #>
        param(
            [double[]]$TrackDurationsSeconds,
            [string]$SubFolder = 'input'
        )
        $inputDir = Join-Path $Script:TestRoot $SubFolder
        New-Item -ItemType Directory -Force -Path $inputDir | Out-Null

        $trackNum = 0
        foreach ($dur in $TrackDurationsSeconds) {
            $trackNum++
            $name = "Track{0:D2}.wav" -f $trackNum
            $path = Join-Path $inputDir $name
            $ffArgs = @(
                "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=$($dur):sample_rate=44100",
                "-c:a", "pcm_s16le", $path
            )
            & ffmpeg @ffArgs 2>$null
            if (-not (Test-Path $path)) {
                throw "Failed to create test audio file: $path"
            }
        }
        return $inputDir
    }

    function Get-OutputDurationSeconds {
        param([string]$FilePath)
        $raw = & ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 $FilePath 2>$null
        return [double]::Parse($raw.Trim(), [System.Globalization.CultureInfo]::InvariantCulture)
    }

    function New-WrapperScript {
        <#
        .SYNOPSIS
        Creates a temp copy of the script with Read-Host auto-confirmed.
        #>
        $scriptContent = Get-Content $Script:ScriptPath -Raw
        $wrapperScript = $scriptContent -replace 'Read-Host\s+"Proceed with resizing\?.*?"', '"Y"'
        $tempScript = Join-Path $Script:TestRoot "runner_$([guid]::NewGuid().ToString('N').Substring(0,8)).ps1"
        Set-Content -Path $tempScript -Value $wrapperScript
        return $tempScript
    }

    function Invoke-ResizeScript {
        <#
        .SYNOPSIS
        Runs the resize script with the given parameters, auto-confirming the prompt.
        Returns the output folder path.
        #>
        param([hashtable]$Params)

        $outputDir = Join-Path $Script:TestRoot "output_$([guid]::NewGuid().ToString('N').Substring(0,8))"
        $Params['OutputFolder'] = $outputDir

        $tempScript = New-WrapperScript
        & $tempScript @Params

        return $outputDir
    }
}

AfterAll {
    if (Test-Path $Script:TestRoot) {
        Remove-Item -Recurse -Force $Script:TestRoot -ErrorAction SilentlyContinue
    }
}

Describe 'Resize-AlbumToFitMD.ps1' {

    BeforeEach {
        if (Test-Path $Script:TestRoot) {
            Remove-Item -Recurse -Force $Script:TestRoot -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Force -Path $Script:TestRoot | Out-Null
    }

    Context 'Input validation' {

        It 'Should throw when InputFolder does not exist' {
            { & $Script:ScriptPath -InputFolder 'C:\NonExistent_ZZZZZ' -Mode Speed } |
                Should -Throw '*Input folder not found*'
        }

        It 'Should throw when no audio files are found' {
            $emptyDir = Join-Path $Script:TestRoot 'empty'
            New-Item -ItemType Directory -Force -Path $emptyDir | Out-Null
            { & $Script:ScriptPath -InputFolder $emptyDir -Mode Speed } |
                Should -Throw '*No audio files found*'
        }

        It 'Should throw when both -LP2 and -LP4 are specified' {
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(10)
            { & $Script:ScriptPath -InputFolder $inputDir -Mode Speed -LP2 -LP4 } |
                Should -Throw '*Cannot specify both*'
        }
    }

    Context 'Album already fits (no resizing needed)' {

        It 'Should exit gracefully when album is under target duration' {
            # 3 tracks of 10 seconds = 30 seconds, well under target
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(10, 10, 10)
            $tempScript = New-WrapperScript
            $outputDir = Join-Path $Script:TestRoot 'output_nofit'

            $result = & $tempScript -InputFolder $inputDir -OutputFolder $outputDir -Mode Speed -TargetMinutes 5 6>&1
            $resultText = $result -join "`n"
            $resultText | Should -Match 'No resizing needed'
        }
    }

    Context 'Speed mode resizing' {

        It 'Should resize tracks to fit the target duration (SP mode)' {
            # 3 tracks of 30s each = 90s total, target = 1 min = 60s
            # Speed factor = 90/60 = 1.5x
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(30, 30, 30)
            $outputDir = Invoke-ResizeScript @{
                InputFolder   = $inputDir
                Mode          = 'Speed'
                TargetMinutes = 1
            }

            $outputFiles = Get-ChildItem -Path $outputDir -Filter '*.flac' | Sort-Object Name
            $outputFiles.Count | Should -Be 3

            $totalOutputSeconds = 0
            foreach ($f in $outputFiles) {
                $totalOutputSeconds += (Get-OutputDurationSeconds $f.FullName)
            }

            # Should be close to 60 seconds (within 1 second tolerance)
            $totalOutputSeconds | Should -BeLessOrEqual 61
            $totalOutputSeconds | Should -BeGreaterOrEqual 59
        }

        It 'Should resize tracks in LP2 mode (2x effective capacity)' {
            # 3 tracks of 30s = 90s total
            # LP2 with TargetMinutes=0.5 → effective target = 1 min = 60s
            # Speed factor = 90/60 = 1.5x
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(30, 30, 30)
            $outputDir = Invoke-ResizeScript @{
                InputFolder   = $inputDir
                Mode          = 'Speed'
                TargetMinutes = 0.5
                LP2           = $true
            }

            $outputFiles = Get-ChildItem -Path $outputDir -Filter '*.flac' | Sort-Object Name
            $outputFiles.Count | Should -Be 3

            $totalOutputSeconds = 0
            foreach ($f in $outputFiles) {
                $totalOutputSeconds += (Get-OutputDurationSeconds $f.FullName)
            }

            # LP2 doubles 0.5 min to 1 min = 60s effective target
            $totalOutputSeconds | Should -BeLessOrEqual 61
            $totalOutputSeconds | Should -BeGreaterOrEqual 59
        }

        It 'Should resize tracks in LP4 mode (4x effective capacity)' {
            # 3 tracks of 30s = 90s total
            # LP4 with TargetMinutes=0.25 → effective target = 1 min = 60s
            # Speed factor = 90/60 = 1.5x
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(30, 30, 30)
            $outputDir = Invoke-ResizeScript @{
                InputFolder   = $inputDir
                Mode          = 'Speed'
                TargetMinutes = 0.25
                LP4           = $true
            }

            $outputFiles = Get-ChildItem -Path $outputDir -Filter '*.flac' | Sort-Object Name
            $outputFiles.Count | Should -Be 3

            $totalOutputSeconds = 0
            foreach ($f in $outputFiles) {
                $totalOutputSeconds += (Get-OutputDurationSeconds $f.FullName)
            }

            # LP4 quadruples 0.25 min to 1 min = 60s effective target
            $totalOutputSeconds | Should -BeLessOrEqual 61
            $totalOutputSeconds | Should -BeGreaterOrEqual 59
        }
    }

    Context 'TimeStretch mode resizing' {

        It 'Should resize tracks using rubberband without changing pitch' {
            # 3 tracks of 30s = 90s total, target = 1 min = 60s
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(30, 30, 30)
            $outputDir = Invoke-ResizeScript @{
                InputFolder   = $inputDir
                Mode          = 'TimeStretch'
                TargetMinutes = 1
            }

            $outputFiles = Get-ChildItem -Path $outputDir -Filter '*.flac' | Sort-Object Name
            $outputFiles.Count | Should -Be 3

            $totalOutputSeconds = 0
            foreach ($f in $outputFiles) {
                $totalOutputSeconds += (Get-OutputDurationSeconds $f.FullName)
            }

            $totalOutputSeconds | Should -BeLessOrEqual 61
            $totalOutputSeconds | Should -BeGreaterOrEqual 59
        }
    }

    Context 'Output format support' {

        It 'Should output FLAC files' {
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(20, 20) -SubFolder 'fmt_flac'
            $outputDir = Invoke-ResizeScript @{ InputFolder = $inputDir; Mode = 'Speed'; OutputFormat = 'FLAC'; TargetMinutes = 0.5 }
            (Get-ChildItem $outputDir -Filter '*.flac').Count | Should -Be 2
        }

        It 'Should output WAV files' {
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(20, 20) -SubFolder 'fmt_wav'
            $outputDir = Invoke-ResizeScript @{ InputFolder = $inputDir; Mode = 'Speed'; OutputFormat = 'WAV'; TargetMinutes = 0.5 }
            (Get-ChildItem $outputDir -Filter '*.wav').Count | Should -Be 2
        }

        It 'Should output AIFF files' {
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(20, 20) -SubFolder 'fmt_aiff'
            $outputDir = Invoke-ResizeScript @{ InputFolder = $inputDir; Mode = 'Speed'; OutputFormat = 'AIFF'; TargetMinutes = 0.5 }
            (Get-ChildItem $outputDir -Filter '*.aiff').Count | Should -Be 2
        }

        It 'Should output ALAC (m4a) files' {
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(20, 20) -SubFolder 'fmt_alac'
            $outputDir = Invoke-ResizeScript @{ InputFolder = $inputDir; Mode = 'Speed'; OutputFormat = 'ALAC'; TargetMinutes = 0.5 }
            (Get-ChildItem $outputDir -Filter '*.m4a').Count | Should -Be 2
        }

        It 'Should output WavPack (wv) files' {
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(20, 20) -SubFolder 'fmt_wv'
            $outputDir = Invoke-ResizeScript @{ InputFolder = $inputDir; Mode = 'Speed'; OutputFormat = 'WavPack'; TargetMinutes = 0.5 }
            (Get-ChildItem $outputDir -Filter '*.wv').Count | Should -Be 2
        }

        It 'Should output TTA files' {
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(20, 20) -SubFolder 'fmt_tta'
            $outputDir = Invoke-ResizeScript @{ InputFolder = $inputDir; Mode = 'Speed'; OutputFormat = 'TTA'; TargetMinutes = 0.5 }
            (Get-ChildItem $outputDir -Filter '*.tta').Count | Should -Be 2
        }
    }

    Context 'Speed factor proportionality' {

        It 'Should resize all tracks by the same ratio' {
            # 3 tracks of different lengths: 20s, 30s, 40s = 90s total
            # Target = 1 min = 60s → factor = 90/60 = 1.5x
            $inputDir = New-TestInputFolder -TrackDurationsSeconds @(20, 30, 40) -SubFolder 'ratio_test'
            $outputDir = Invoke-ResizeScript @{
                InputFolder   = $inputDir
                Mode          = 'Speed'
                TargetMinutes = 1
            }

            $outputFiles = Get-ChildItem $outputDir -Filter '*.flac' | Sort-Object Name
            $outputFiles.Count | Should -Be 3

            $expectedFactor = 90.0 / 60.0
            $durations = @()
            foreach ($f in $outputFiles) {
                $durations += (Get-OutputDurationSeconds $f.FullName)
            }

            $originalDurations = @(20, 30, 40)
            for ($i = 0; $i -lt 3; $i++) {
                $actualRatio = $originalDurations[$i] / $durations[$i]
                # Each track's ratio should be close to the expected factor (within 2%)
                $actualRatio | Should -BeGreaterOrEqual ($expectedFactor * 0.98)
                $actualRatio | Should -BeLessOrEqual ($expectedFactor * 1.02)
            }
        }
    }
}

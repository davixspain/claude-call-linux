// aec_bridge — ponte de audio full-duplex com AEC do macOS (Voice Processing I/O).
//
// O MESMO AVAudioEngine captura o mic E toca o TTS, com setVoiceProcessingEnabled(true).
// Isso liga o AEC/supressao de eco do SO (o mesmo do FaceTime/Siri): o que toca pelo
// alto-falante e cancelado do que o mic capta -> da pra interromper o agente SEM FONE.
//
// Protocolo (stdio, PCM 24 kHz mono Int16 LE):
//   stdin  <- audio do TTS (Python) pra tocar
//   stdout -> audio do mic (ja com AEC) pro Python
//   stderr -> logs
//
// Compilar: swiftc -O aec_bridge.swift -o aecbridge

import AVFoundation
import Foundation

let kMicRate: Double = 16000    // mic -> stdout: VAD (Silero) e whisper querem 16k
let kSpkRate: Double = 24000    // stdin -> playback: edge-tts e 24k

func elog(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }

let micInt16 = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: kMicRate, channels: 1, interleaved: true)!
let spkInt16 = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: kSpkRate, channels: 1, interleaved: true)!
let spkFloat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: kSpkRate, channels: 1, interleaved: false)!

let engine = AVAudioEngine()
let player = AVAudioPlayerNode()
let inputNode = engine.inputNode

// Liga o AEC ANTES de configurar/iniciar (acopla input+output no SO).
do {
    try inputNode.setVoiceProcessingEnabled(true)
    elog("voice processing (AEC) ON")
} catch {
    elog("setVoiceProcessingEnabled falhou: \(error)")
}

engine.attach(player)
engine.connect(player, to: engine.mainMixerNode, format: spkFloat)

// --- MIC -> stdout (com AEC), 16 kHz ---
let inFmt = inputNode.outputFormat(forBus: 0)   // formato apos voice processing
guard let toMic = AVAudioConverter(from: inFmt, to: micInt16) else {
    elog("converter mic falhou"); exit(1)
}
let stdoutHandle = FileHandle.standardOutput

inputNode.installTap(onBus: 0, bufferSize: 1024, format: inFmt) { buffer, _ in
    let cap = AVAudioFrameCount(Double(buffer.frameLength) * kMicRate / inFmt.sampleRate + 32)
    guard let out = AVAudioPCMBuffer(pcmFormat: micInt16, frameCapacity: cap) else { return }
    var err: NSError?
    var fed = false
    _ = toMic.convert(to: out, error: &err) { _, status in
        if fed { status.pointee = .noDataNow; return nil }
        fed = true; status.pointee = .haveData; return buffer
    }
    if let ch = out.int16ChannelData, out.frameLength > 0 {
        stdoutHandle.write(Data(bytes: ch[0], count: Int(out.frameLength) * 2))
    }
}

// --- stdin (TTS, 24 kHz) -> playback ---
guard let toSpk = AVAudioConverter(from: spkInt16, to: spkFloat) else {
    elog("converter playback falhou"); exit(1)
}
DispatchQueue.global(qos: .userInitiated).async {
    let chunkBytes = 1024 * 2
    let stdin = FileHandle.standardInput
    while true {
        let data = stdin.readData(ofLength: chunkBytes)
        if data.isEmpty { break }              // EOF: Python fechou
        let frames = data.count / 2
        if frames == 0 { continue }
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: spkInt16, frameCapacity: AVAudioFrameCount(frames)),
              let outBuf = AVAudioPCMBuffer(pcmFormat: spkFloat, frameCapacity: AVAudioFrameCount(frames)) else { continue }
        inBuf.frameLength = AVAudioFrameCount(frames)
        data.withUnsafeBytes { raw in
            memcpy(inBuf.int16ChannelData![0], raw.baseAddress!, data.count)
        }
        outBuf.frameLength = AVAudioFrameCount(frames)
        var err: NSError?
        var fed = false
        _ = toSpk.convert(to: outBuf, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true; status.pointee = .haveData; return inBuf
        }
        player.scheduleBuffer(outBuf, completionHandler: nil)
    }
}

engine.prepare()
do {
    try engine.start()
    player.play()
    elog("aecbridge ready (mic out=\(Int(kMicRate))Hz, spk in=\(Int(kSpkRate))Hz, hw in=\(inFmt.sampleRate)Hz)")
} catch {
    elog("engine start falhou: \(error)")
    elog("DICA: erro -10875 normalmente e sessao de audio sem dispositivo (headless) ou")
    elog("      falta de permissao de microfone. Rode do seu Terminal logado, com mic liberado.")
    exit(1)
}

RunLoop.main.run()

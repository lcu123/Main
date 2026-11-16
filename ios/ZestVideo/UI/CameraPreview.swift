import AVFoundation
import AVKit
import SwiftUI
import UIKit

/// The live camera picture, filling the screen.
///
/// It also listens for the phone's volume buttons (iOS 17.2 and later), so a
/// tech whose gloves do not register on glass can still start a recording.
struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    /// A volume button was pressed and let go.
    let onCaptureButton: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(action: onCaptureButton)
    }

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.backgroundColor = .black
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspectFill

        if #available(iOS 17.2, *) {
            let coordinator = context.coordinator
            let interaction = AVCaptureEventInteraction { event in
                if event.phase == .ended {
                    coordinator.action()
                }
            }
            view.addInteraction(interaction)
        }
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {
        context.coordinator.action = onCaptureButton
        if uiView.previewLayer.session !== session {
            uiView.previewLayer.session = session
        }
    }

    final class Coordinator {
        var action: () -> Void

        init(action: @escaping () -> Void) {
            self.action = action
        }
    }
}

/// A view whose backing layer is the camera preview, so it resizes for free.
final class PreviewView: UIView {
    override class var layerClass: AnyClass {
        AVCaptureVideoPreviewLayer.self
    }

    var previewLayer: AVCaptureVideoPreviewLayer {
        // The layer class is fixed above, so this cast cannot fail.
        layer as! AVCaptureVideoPreviewLayer
    }
}

/// Plays a clip from its file with no controls to fiddle with. The video is
/// streamed from disk, never loaded into memory.
struct ClipPlayerView: UIViewRepresentable {
    let player: AVPlayer

    func makeUIView(context: Context) -> PlayerLayerView {
        let view = PlayerLayerView()
        view.backgroundColor = .black
        view.playerLayer.player = player
        view.playerLayer.videoGravity = .resizeAspect
        return view
    }

    func updateUIView(_ uiView: PlayerLayerView, context: Context) {
        if uiView.playerLayer.player !== player {
            uiView.playerLayer.player = player
        }
    }

    static func dismantleUIView(_ uiView: PlayerLayerView, coordinator: ()) {
        uiView.playerLayer.player = nil
    }
}

final class PlayerLayerView: UIView {
    override class var layerClass: AnyClass {
        AVPlayerLayer.self
    }

    var playerLayer: AVPlayerLayer {
        layer as! AVPlayerLayer
    }
}

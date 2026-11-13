#if targetEnvironment(simulator)
import SwiftUI

/// A simple drawing of a house on a lawn. **Simulator only.**
///
/// The simulator has no camera, so screenshot mode draws this where the
/// camera picture or the recorded clip would be. It is compiled out of any
/// build that can run on a real phone.
struct StandInPicture: View {
    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            let height = geometry.size.height
            ZStack {
                // Sky.
                LinearGradient(
                    colors: [Color(hex: 0x4A9BE8), Color(hex: 0xBFE3FA)],
                    startPoint: .top, endPoint: .bottom
                )
                // Sun.
                Circle()
                    .fill(Color(hex: 0xFFF7C2))
                    .frame(width: width * 0.16, height: width * 0.16)
                    .position(x: width * 0.82, y: height * 0.16)
                // Lawn.
                LinearGradient(
                    colors: [Color(hex: 0x6CC04A), Color(hex: 0x2F8F3A)],
                    startPoint: .top, endPoint: .bottom
                )
                .frame(width: width, height: height * 0.46)
                .position(x: width / 2, y: height * 0.77)
                // House.
                House()
                    .frame(width: width * 0.66, height: height * 0.27)
                    .position(x: width * 0.46, y: height * 0.44)
                // Path to the door.
                PathToDoor()
                    .fill(Color(hex: 0xD9D2C3))
                    .frame(width: width * 0.3, height: height * 0.43)
                    .position(x: width * 0.46, y: height * 0.785)
            }
            .frame(width: width, height: height)
            .clipped()
        }
    }
}

private struct House: View {
    var body: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            let height = geometry.size.height
            ZStack {
                // Walls.
                Rectangle()
                    .fill(Color(hex: 0xF4EBDD))
                    .frame(width: width * 0.86, height: height * 0.58)
                    .position(x: width / 2, y: height * 0.71)
                // Roof.
                Roof()
                    .fill(Color(hex: 0x7A5548))
                    .frame(width: width, height: height * 0.46)
                    .position(x: width / 2, y: height * 0.23)
                // Door.
                Rectangle()
                    .fill(Color(hex: 0x5D4037))
                    .frame(width: width * 0.13, height: height * 0.36)
                    .position(x: width / 2, y: height * 0.82)
                // Windows.
                Rectangle()
                    .fill(Color(hex: 0x9FD3F5))
                    .frame(width: width * 0.17, height: height * 0.2)
                    .position(x: width * 0.24, y: height * 0.68)
                Rectangle()
                    .fill(Color(hex: 0x9FD3F5))
                    .frame(width: width * 0.17, height: height * 0.2)
                    .position(x: width * 0.76, y: height * 0.68)
            }
        }
    }
}

private struct Roof: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.midX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        path.closeSubpath()
        return path
    }
}

private struct PathToDoor: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.midX - rect.width * 0.12, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.midX + rect.width * 0.12, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        path.closeSubpath()
        return path
    }
}
#endif

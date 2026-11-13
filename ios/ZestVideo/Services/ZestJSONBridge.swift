import Foundation
import ZestCore

/// One shared pair of JSON coders (ISO 8601 dates) for disk and network.
enum ZestJSONBridge {
    static let encoder: JSONEncoder = ZestJSON.encoder()
    static let decoder: JSONDecoder = ZestJSON.decoder()
}

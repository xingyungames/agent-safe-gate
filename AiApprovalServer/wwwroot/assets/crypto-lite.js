/*
 * crypto-lite.js —— 纯前端 SHA-256 / HMAC-SHA256 兜底实现。
 *
 * 为什么需要它：请求签名用到了 WebCrypto（crypto.subtle），但 crypto.subtle 只在
 * "安全上下文"（https 或 localhost/127.0.0.1）可用。如果运维把服务挂在局域网 IP 上用
 * http 访问，crypto.subtle 会直接是 undefined，签名就会失败、整个系统不可用。
 * 因此这里提供等价实现作为兜底：功能一致，性能略差（对每请求一次的签名场景无影响）。
 *
 * 注意：这**不是**密码学创新，只是标准算法的直译；安全强度与服务端校验保持一致。
 */
(function (global) {
  'use strict';

  var K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ];

  function utf8Bytes(text) {
    if (global.TextEncoder) return new global.TextEncoder().encode(text);
    // 极端情况下（老浏览器）退回手工编码
    var out = [];
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
      else if (c < 0xd800 || c >= 0xe000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
      else {
        i++;
        var cp = 0x10000 + (((c & 0x3ff) << 10) | (text.charCodeAt(i) & 0x3ff));
        out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
      }
    }
    return new Uint8Array(out);
  }

  function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }

  function sha256Bytes(bytes) {
    var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var len = bytes.length;
    var totalLen = Math.ceil((len + 9) / 64) * 64;
    var msg = new Uint8Array(totalLen);
    msg.set(bytes);
    msg[len] = 0x80;

    // 长度以比特计，写入最后 8 字节（big-endian，64 位）
    var bitLenHi = Math.floor(len / 536870912);      // len*8 / 2^32
    var bitLenLo = (len * 8) >>> 0;
    msg[totalLen - 8] = (bitLenHi >>> 24) & 255;
    msg[totalLen - 7] = (bitLenHi >>> 16) & 255;
    msg[totalLen - 6] = (bitLenHi >>> 8) & 255;
    msg[totalLen - 5] = bitLenHi & 255;
    msg[totalLen - 4] = (bitLenLo >>> 24) & 255;
    msg[totalLen - 3] = (bitLenLo >>> 16) & 255;
    msg[totalLen - 2] = (bitLenLo >>> 8) & 255;
    msg[totalLen - 1] = bitLenLo & 255;

    var w = new Int32Array(64);

    for (var offset = 0; offset < totalLen; offset += 64) {
      for (var i = 0; i < 16; i++) {
        w[i] = (msg[offset + i * 4] << 24) | (msg[offset + i * 4 + 1] << 16) |
               (msg[offset + i * 4 + 2] << 8) | (msg[offset + i * 4 + 3]);
      }
      for (i = 16; i < 64; i++) {
        var s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        var s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }

      var a = H[0], b = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];

      for (i = 0; i < 64; i++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var temp1 = (h + S1 + ch + K[i] + w[i]) | 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var temp2 = (S0 + maj) | 0;

        h = g; g = f; f = e;
        e = (d + temp1) | 0;
        d = c; c = b; b = a;
        a = (temp1 + temp2) | 0;
      }

      H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0; H[2] = (H[2] + c) | 0; H[3] = (H[3] + d) | 0;
      H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0; H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0;
    }

    var out = new Uint8Array(32);
    for (i = 0; i < 8; i++) {
      out[i * 4] = (H[i] >>> 24) & 255;
      out[i * 4 + 1] = (H[i] >>> 16) & 255;
      out[i * 4 + 2] = (H[i] >>> 8) & 255;
      out[i * 4 + 3] = H[i] & 255;
    }
    return out;
  }

  function hmacSha256Bytes(keyBytes, msgBytes) {
    var blockSize = 64;
    var key = keyBytes;
    if (key.length > blockSize) key = sha256Bytes(key);

    var inner = new Uint8Array(blockSize + msgBytes.length);
    var outer = new Uint8Array(blockSize + 32);
    for (var i = 0; i < blockSize; i++) {
      var k = i < key.length ? key[i] : 0;
      inner[i] = k ^ 0x36;
      outer[i] = k ^ 0x5c;
    }
    inner.set(msgBytes, blockSize);
    outer.set(sha256Bytes(inner), blockSize);
    return sha256Bytes(outer);
  }

  function toHex(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16);
    return s;
  }

  function toBase64(bytes) {
    if (typeof btoa === 'function') {
      var binary = '';
      for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
      return btoa(binary);
    }
    return Buffer.from(bytes).toString('base64');
  }

  global.CryptoLite = {
    utf8Bytes: utf8Bytes,
    sha256Bytes: sha256Bytes,
    sha256Hex: function (text) { return toHex(sha256Bytes(utf8Bytes(text))); },
    hmacSha256Base64: function (keyText, messageText) {
      return toBase64(hmacSha256Bytes(utf8Bytes(keyText), utf8Bytes(messageText)));
    }
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);

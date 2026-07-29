#include "json.hpp"

#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <limits>
#include <sstream>

namespace voz_neural::json {
namespace {

constexpr std::size_t kMaximumJsonBytes = 64 * 1024;

class Parser final {
 public:
  explicit Parser(std::string_view source) : source_(source) {
    if (source_.size() > kMaximumJsonBytes) {
      throw ParseError("JSON request exceeds 64 KiB");
    }
  }

  Object Parse() {
    SkipWhitespace();
    Expect('{');
    SkipWhitespace();

    Object object;
    if (Consume('}')) {
      Finish();
      return object;
    }

    while (true) {
      SkipWhitespace();
      if (Peek() != '"') {
        Fail("object key must be a string");
      }
      std::string key = ParseString();
      SkipWhitespace();
      Expect(':');
      SkipWhitespace();
      Value value = ParseValue();
      if (!object.emplace(std::move(key), std::move(value)).second) {
        Fail("duplicate object key");
      }
      SkipWhitespace();
      if (Consume('}')) {
        break;
      }
      Expect(',');
    }

    Finish();
    return object;
  }

 private:
  [[noreturn]] void Fail(const char* message) const {
    std::ostringstream formatted;
    formatted << message << " at byte " << cursor_;
    throw ParseError(formatted.str());
  }

  char Peek() const {
    return cursor_ < source_.size() ? source_[cursor_] : '\0';
  }

  char Take() {
    if (cursor_ >= source_.size()) {
      Fail("unexpected end of JSON");
    }
    return source_[cursor_++];
  }

  bool Consume(char expected) {
    if (Peek() != expected) {
      return false;
    }
    ++cursor_;
    return true;
  }

  void Expect(char expected) {
    if (!Consume(expected)) {
      Fail("unexpected JSON token");
    }
  }

  void SkipWhitespace() {
    while (cursor_ < source_.size()) {
      const char value = source_[cursor_];
      if (value != ' ' && value != '\t' && value != '\n' && value != '\r') {
        break;
      }
      ++cursor_;
    }
  }

  void Finish() {
    SkipWhitespace();
    if (cursor_ != source_.size()) {
      Fail("trailing data after JSON object");
    }
  }

  static int HexDigit(char value) {
    if (value >= '0' && value <= '9') {
      return value - '0';
    }
    if (value >= 'a' && value <= 'f') {
      return 10 + value - 'a';
    }
    if (value >= 'A' && value <= 'F') {
      return 10 + value - 'A';
    }
    return -1;
  }

  std::uint32_t ParseHexQuad() {
    std::uint32_t result = 0;
    for (int index = 0; index < 4; ++index) {
      const int digit = HexDigit(Take());
      if (digit < 0) {
        Fail("invalid Unicode escape");
      }
      result = (result << 4U) | static_cast<std::uint32_t>(digit);
    }
    return result;
  }

  static void AppendUtf8(std::uint32_t code_point, std::string& output) {
    if (code_point <= 0x7FU) {
      output.push_back(static_cast<char>(code_point));
    } else if (code_point <= 0x7FFU) {
      output.push_back(static_cast<char>(0xC0U | (code_point >> 6U)));
      output.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
    } else if (code_point <= 0xFFFFU) {
      output.push_back(static_cast<char>(0xE0U | (code_point >> 12U)));
      output.push_back(
          static_cast<char>(0x80U | ((code_point >> 6U) & 0x3FU)));
      output.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
    } else {
      output.push_back(static_cast<char>(0xF0U | (code_point >> 18U)));
      output.push_back(
          static_cast<char>(0x80U | ((code_point >> 12U) & 0x3FU)));
      output.push_back(
          static_cast<char>(0x80U | ((code_point >> 6U) & 0x3FU)));
      output.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
    }
  }

  std::string ParseString() {
    Expect('"');
    std::string result;
    while (true) {
      const unsigned char value = static_cast<unsigned char>(Take());
      if (value == '"') {
        return result;
      }
      if (value < 0x20U) {
        Fail("unescaped control character in string");
      }
      if (value != '\\') {
        result.push_back(static_cast<char>(value));
        continue;
      }

      switch (Take()) {
        case '"':
          result.push_back('"');
          break;
        case '\\':
          result.push_back('\\');
          break;
        case '/':
          result.push_back('/');
          break;
        case 'b':
          result.push_back('\b');
          break;
        case 'f':
          result.push_back('\f');
          break;
        case 'n':
          result.push_back('\n');
          break;
        case 'r':
          result.push_back('\r');
          break;
        case 't':
          result.push_back('\t');
          break;
        case 'u': {
          std::uint32_t code_point = ParseHexQuad();
          if (code_point >= 0xD800U && code_point <= 0xDBFFU) {
            if (Take() != '\\' || Take() != 'u') {
              Fail("high surrogate is not followed by a low surrogate");
            }
            const std::uint32_t low = ParseHexQuad();
            if (low < 0xDC00U || low > 0xDFFFU) {
              Fail("invalid low surrogate");
            }
            code_point =
                0x10000U + ((code_point - 0xD800U) << 10U) + (low - 0xDC00U);
          } else if (code_point >= 0xDC00U && code_point <= 0xDFFFU) {
            Fail("unexpected low surrogate");
          }
          AppendUtf8(code_point, result);
          break;
        }
        default:
          Fail("invalid string escape");
      }
    }
  }

  Value ParseNumber() {
    const std::size_t start = cursor_;
    Consume('-');
    if (Consume('0')) {
      if (Peek() >= '0' && Peek() <= '9') {
        Fail("leading zero in number");
      }
    } else {
      if (Peek() < '1' || Peek() > '9') {
        Fail("invalid number");
      }
      while (Peek() >= '0' && Peek() <= '9') {
        ++cursor_;
      }
    }
    if (Consume('.')) {
      if (Peek() < '0' || Peek() > '9') {
        Fail("fraction has no digits");
      }
      while (Peek() >= '0' && Peek() <= '9') {
        ++cursor_;
      }
    }
    if (Peek() == 'e' || Peek() == 'E') {
      ++cursor_;
      if (Peek() == '+' || Peek() == '-') {
        ++cursor_;
      }
      if (Peek() < '0' || Peek() > '9') {
        Fail("exponent has no digits");
      }
      while (Peek() >= '0' && Peek() <= '9') {
        ++cursor_;
      }
    }

    const std::string token(source_.substr(start, cursor_ - start));
    char* parsed_end = nullptr;
    errno = 0;
    const double number = std::strtod(token.c_str(), &parsed_end);
    if (errno == ERANGE || parsed_end != token.c_str() + token.size() ||
        !std::isfinite(number)) {
      Fail("number is outside the supported finite range");
    }
    Value result;
    result.kind = Value::Kind::kNumber;
    result.number_value = number;
    return result;
  }

  void ExpectLiteral(std::string_view literal) {
    if (source_.substr(cursor_, literal.size()) != literal) {
      Fail("invalid JSON literal");
    }
    cursor_ += literal.size();
  }

  Value ParseValue() {
    Value result;
    switch (Peek()) {
      case '"':
        result.kind = Value::Kind::kString;
        result.string_value = ParseString();
        return result;
      case 't':
        ExpectLiteral("true");
        result.kind = Value::Kind::kBoolean;
        result.boolean_value = true;
        return result;
      case 'f':
        ExpectLiteral("false");
        result.kind = Value::Kind::kBoolean;
        result.boolean_value = false;
        return result;
      case 'n':
        ExpectLiteral("null");
        result.kind = Value::Kind::kNull;
        return result;
      default:
        if (Peek() == '-' || (Peek() >= '0' && Peek() <= '9')) {
          return ParseNumber();
        }
        Fail("only flat JSON scalar values are accepted");
    }
  }

  std::string_view source_;
  std::size_t cursor_ = 0;
};

}  // namespace

Object ParseObject(std::string_view source) {
  return Parser(source).Parse();
}

std::string Quote(std::string_view value) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string output;
  output.reserve(value.size() + 2);
  output.push_back('"');
  for (const unsigned char byte : value) {
    switch (byte) {
      case '"':
        output += "\\\"";
        break;
      case '\\':
        output += "\\\\";
        break;
      case '\b':
        output += "\\b";
        break;
      case '\f':
        output += "\\f";
        break;
      case '\n':
        output += "\\n";
        break;
      case '\r':
        output += "\\r";
        break;
      case '\t':
        output += "\\t";
        break;
      default:
        if (byte < 0x20U) {
          output += "\\u00";
          output.push_back(kHex[byte >> 4U]);
          output.push_back(kHex[byte & 0x0FU]);
        } else {
          output.push_back(static_cast<char>(byte));
        }
    }
  }
  output.push_back('"');
  return output;
}

}  // namespace voz_neural::json

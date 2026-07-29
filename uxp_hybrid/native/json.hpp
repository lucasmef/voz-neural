#pragma once

#include <map>
#include <stdexcept>
#include <string>
#include <string_view>

namespace voz_neural::json {

class ParseError final : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

struct Value {
  enum class Kind {
    kNull,
    kString,
    kNumber,
    kBoolean,
  };

  Kind kind = Kind::kNull;
  std::string string_value;
  double number_value = 0.0;
  bool boolean_value = false;
};

using Object = std::map<std::string, Value, std::less<>>;

Object ParseObject(std::string_view source);
std::string Quote(std::string_view value);

}  // namespace voz_neural::json

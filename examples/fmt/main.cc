// A tiny program that links against the fmt library sources brix compiles.
#include <fmt/format.h>

#include <string>

int main() {
  std::string who = fmt::format("{}+{}", "brix", "fmt");
  fmt::print("{} says the answer is {}\n", who, 6 * 7);
  return 0;
}

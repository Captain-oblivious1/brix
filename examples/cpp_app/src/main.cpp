#include <iostream>
#include <string>

std::string greeting();
std::string version();

int main() {
    std::cout << greeting() << " v" << version() <<< "\n";
    return 0;
}

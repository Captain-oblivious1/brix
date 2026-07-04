// Reads a CSV of "ClassName,text" rows and emits one Java class per row.
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: schema_generator <schema.csv> <out_dir>\n";
        return 1;
    }
    std::ifstream in(argv[1]);
    if (!in) {
        std::cerr << "cannot open " << argv[1] << "\n";
        return 1;
    }
    std::filesystem::create_directories(argv[2]);
    std::string line;
    while (std::getline(in, line)) {
        auto comma = line.find(',');
        if (comma == std::string::npos) continue;
        std::string name = line.substr(0, comma);
        std::string text = line.substr(comma + 1);
        std::ofstream out(std::string(argv[2]) + "/" + name + ".java");
        out << "public class " << name << " {\n"
            << "    public static String describe() { return \"" << text << "\"; }\n"
            << "}\n";
    }
    return 0;
}

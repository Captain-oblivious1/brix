#include <gtest/gtest.h>
#include "greeter.h"
TEST(Greeter, greetsByName) { EXPECT_EQ(greet("World"), "Hello, World!"); }

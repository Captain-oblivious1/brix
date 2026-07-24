#include <gtest/gtest.h>
#include "adder.h"
TEST(Adder, addsTwoPositives) { EXPECT_EQ(add(2, 3), 5); }
TEST(Adder, addsANegative) { EXPECT_EQ(add(2, -3), -1); }

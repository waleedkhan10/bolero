cmake_minimum_required(VERSION 2.8)


set(CMAKE_MODULE_PATH ${CMAKE_MODULE_PATH} "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

find_package(Eigen3)

enable_testing()

add_subdirectory(src)
add_subdirectory(dmp)
